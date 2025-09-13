# theanotools_py3.py
# Python 3 rewrite of theanotools.py (originally Python 2)

from __future__ import annotations

import numpy as np

try:
    import theano
    import theano.tensor as T
except Exception as e:
    raise ImportError(
        "Theano is not installed. On Python 3, install 'theano-pymc' (pip install theano-pymc)."
    ) from e


def sym_tiling_index(
    X,
    input_index,
    ntiles,
    ntilings,
    state_range,
    offset=None,
    hashing=None,
):
    """
    Symbolic (Theano) tiling index computation for one tiling set.
    X: TensorType (N, D, 1)
    """
    # Copy and expand ranges for selected dimensions
    s_range0 = state_range[0][input_index].copy()
    s_range1 = state_range[1][input_index].copy()
    # expand min to allow negative fractional offsets
    s_range0 = s_range0 - (s_range1 - s_range0) / (ntiles - 1)

    if isinstance(ntiles, int):
        ntiles = np.array([ntiles] * len(input_index), dtype=np.uint32)

    if offset is None:
        offset = np.empty((ntiles.shape[0], int(ntilings)), dtype=float)
        for i in range(ntiles.shape[0]):
            # uniformly spaced negative offsets in (-1/ntiles[i], 0]
            offset[i, :] = -np.linspace(0, 1.0 / float(ntiles[i]), int(ntilings), endpoint=False)

    if hashing is None:
        hashing = Theano_IdentityHash(ntiles)

    input_index = np.array(input_index, dtype=np.uint32)
    size = int(ntilings) * int(hashing.memory)
    index_offset = (int(hashing.memory) * np.arange(int(ntilings))).astype("int32")

    # normalize state into [0,1] wrt ranges; ensure broadcasting shape
    s0 = s_range0[None, :, None]
    s1 = s_range1[None, :, None]
    nX = (X[:, input_index, :] - s0) / (s1 - s0)

    indices = T.cast(((offset[None, :, :] + nX) * ntiles[None, :, None]), "int32")
    hashed_index = hashing.getHashedFunction(indices) + index_offset[None, :]
    return hashed_index, size


class Theano_IdentityHash(object):
    def __init__(self, dims):
        self.dims = np.asarray(dims, dtype=int)
        self.memory = int(np.prod(self.dims))

    def getHashedFunction(self, indices):
        """
        Linearizes multi-dim indices (cartesian product) in row-major order.
        indices: (N, D, T)
        """
        # multipliers for each dimension (row-major)
        dims = np.cumprod(np.hstack(([1], self.dims[::-1][:-1])), dtype=int)[::-1]
        dims = dims[None, None, :]  # shape (1, 1, D)
        return T.sum(indices * T.cast(dims, "int32"), axis=1, keepdims=False)


class Theano_UNH(object):
    """
    Universal hashing with a prebuilt random sequence table, using Theano tensors.
    """
    increment = 470

    def __init__(self, input_size, memory):
        self.input_size = int(input_size)
        self.memory = int(memory)

        # Build a 16384-entry random sequence of 32-bit ints from 4x 8-bit chunks
        rndseq = np.zeros(16384, dtype=np.int64)
        for _ in range(4):
            chunk = np.random.randint(np.iinfo("int16").min, np.iinfo("int16").max + 1, 16384, dtype=np.int64) & 0xFF
            rndseq = (rndseq << 8) | chunk

        # Keep as shared for Theano graph
        self.rndseq = theano.shared(rndseq.astype("int64"), borrow=False)

    def getHashedFunction(self, indices):
        rnd_seq = self.rndseq  # shared tensor (16384,)
        a = T.cast(self.increment * T.arange(self.input_size), "int64")  # (D,)

        # (N, D, T)
        index = T.cast(indices, "int64") + a[None, :, None]

        # modulo rnd_seq.size without float ops
        size64 = T.constant(rnd_seq_size(self.rndseq), dtype="int64")
        index = index % size64

        # Gather from random sequence and sum over dims
        gathered = rnd_seq[index]  # (N, D, T)
        hashed_index = T.sum(gathered, axis=1, keepdims=False)  # (N, T), int64

        mem = T.constant(int(self.memory), dtype="int64")
        return T.cast(hashed_index % mem, "int32")


def rnd_seq_size(shared_arr):
    """Small helper to get shared array length in Python int at graph-build time."""
    # Theano shared has .get_value(); we only need size once during graph construction
    return int(shared_arr.get_value(borrow=True).shape[0])


class Theano_Tiling(object):
    """
    Theano-compiled tiling projector producing integer tile indices.
    """

    def __init__(
        self,
        input_indicies,  # sic: kept original name for compatibility
        ntiles,
        ntilings,
        hashing,
        state_range,
        bias_term=True,
    ):
        if hashing is None:
            hashing = [None] * len(ntilings)

        # X has shape (N, D, 1)
        X = T.TensorType(dtype=theano.config.floatX, broadcastable=(False, False, True))("X")
        # provide a test value for Theano shape inference (optional but helpful)
        X.tag.test_value = np.random.rand(1, 2, 1).astype(theano.config.floatX)

        # Build symbolic tilings
        tilings, sizes = zip(
            *[
                sym_tiling_index(X, in_index, nt, t, state_range, hashing=h)
                for in_index, nt, t, h in zip(input_indicies, ntiles, ntilings, hashing)
            ]
        )

        self.__size = int(sum(sizes))

        # Base offsets for each tiling set
        index_offset = np.zeros(len(ntilings), dtype=int)
        if len(sizes) > 1:
            index_offset[1:] = np.cumsum(sizes[:-1], dtype=int)
        # Repeat each base offset t times per set
        index_offset = np.hstack([np.full(t, off, dtype=int) for off, t in zip(index_offset, ntilings)])

        all_indices = T.cast(T.concatenate(tilings, axis=1), "int32") + T.cast(index_offset, "int32")[None, :]
        if bias_term:
            bias_col = (T.ones((all_indices.shape[0], 1), dtype="int32") * T.cast(self.__size, "int32"))
            all_indices = T.concatenate((all_indices, bias_col), axis=1)
            self.__size += 1

        # Compile projector function
        self.proj = theano.function([X], all_indices, allow_input_downcast=True)

    def __call__(self, state: np.ndarray) -> np.ndarray:
        if state.ndim == 1:
            return self.proj(state[None, :, None])[0, :]
        return self.proj(state[:, :, None])

    @property
    def size(self) -> int:
        return int(self.__size)
