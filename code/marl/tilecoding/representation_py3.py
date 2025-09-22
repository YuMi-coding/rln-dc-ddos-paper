# representation_py3.py
# Python 3 rewrite of tilecoding representation (originally Python 2)

from __future__ import annotations

import numpy as np
import scipy.sparse as sp

from scipy.sparse import csr_matrix
from itertools import chain
from typing import Iterable, List, Optional, Sequence, Tuple, Union


"""
authors: Clement Gehring
contact: gehring@csail.mit.edu
date: May 2015
"""


################## VARIOUS INTERFACES #######################################
class Projector(object):
    """Generic map from a state (array) to a feature vector (array)."""
    def __init__(self) -> None:
        pass

    def __call__(self, state: np.ndarray) -> np.ndarray:
        """Project a 1-D or 2-D state array to features."""
        raise NotImplementedError("Subclasses should implement this!")

    @property
    def size(self) -> int:
        raise NotImplementedError("Subclasses should implement this!")


class StateActionProjector(object):
    """Generic map from a (state, action) pair to a feature vector."""
    def __init__(self) -> None:
        pass

    def __call__(self, state: np.ndarray, action: np.ndarray) -> np.ndarray:
        """Project 1-D/2-D state & action arrays to features."""
        raise NotImplementedError("Subclasses should implement this!")

    @property
    def size(self) -> int:
        raise NotImplementedError("Subclasses should implement this!")


class Hashing(object):
    """Hash an array of indices to a single index (for tile coding)."""
    def __init__(self, **kargs) -> None:
        pass

    def __call__(self, indices: np.ndarray) -> np.ndarray:
        """Map per-dimension indices to a single tile index. Accepts 2-D arrays."""
        raise NotImplementedError("Subclasses should implement this!")


################## HELPERS: STATE→(STATE,ACTION) #############################
def tile_and_adjust_state_action(state: np.ndarray, action: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Ensure both arrays are 2-D and share the same row count."""
    if state.ndim == 1:
        state = state.reshape((1, -1))
    if action.ndim == 1:
        action = action.reshape((1, -1))

    if state.shape[0] == 1 and action.shape[0] > 1:
        state = np.tile(state, (action.shape[0], 1))
    elif action.shape[0] == 1 and state.shape[0] > 1:
        action = np.tile(action, (state.shape[0], 1))

    return state, action


class ConcatStateAction(StateActionProjector):
    """State-action projector that concatenates action dims to state dims."""
    def __init__(self, projector: Projector) -> None:
        self.projector = projector

    def __call__(self, state: np.ndarray, action: np.ndarray) -> np.ndarray:
        state, action = tile_and_adjust_state_action(state, action)
        sa = np.hstack((state, action))
        return self.projector(sa)

    @property
    def size(self) -> int:
        return self.projector.size


class RemoveAction(StateActionProjector):
    """State-action projector that ignores actions (value-function use)."""
    def __init__(self, projector: Projector) -> None:
        self.projector = projector

    def __call__(self, state: np.ndarray, action: np.ndarray) -> np.ndarray:
        state, action = tile_and_adjust_state_action(state, action)
        return self.projector(state)

    @property
    def size(self) -> int:
        return self.projector.size


class TabularAction(StateActionProjector):
    """
    Create a tabular action representation around a state projector.
    Output is n*num_actions (sparse by default). If action i is given, the
    block [n*i:n*(i+1)) contains the projected state; others are zero.
    """
    def __init__(self, projector: Projector, num_actions: int, sparse: bool = True) -> None:
        self.phi = projector
        self.__size = self.phi.size * num_actions
        self.num_actions = int(num_actions)
        self.sparse = bool(sparse)

    def __call__(self, state: np.ndarray, action: np.ndarray) -> Union[np.ndarray, csr_matrix]:
        state, action = tile_and_adjust_state_action(state, action)
        phi_s = csr_matrix(self.phi(state))

        # assumes each row has same number of non-zeros
        nnz_per_row = phi_s.indptr[1] - phi_s.indptr[0]
        action = np.tile(action, (1, nnz_per_row)).reshape(-1).astype(int)

        phi_sa = csr_matrix(
            (phi_s.data,
             phi_s.indices + action * self.phi.size,
             phi_s.indptr),
            shape=(phi_s.shape[0], self.size)
        )
        if not self.sparse:
            phi_sa = phi_sa.toarray()
        return phi_sa

    @property
    def size(self) -> int:
        return self.__size


################## INDEX→VECTOR WRAPPERS #####################################
class IndexToBinarySparse(Projector):
    """Wrap an index projector to produce CSR binary (or scaled) vectors."""
    def __init__(self, index_projector: Projector, normalize: bool = False) -> None:
        super().__init__()
        self.index_projector = index_projector
        self.normalize = bool(normalize)
        if normalize:
            self.entry_value = 1.0 / np.sqrt(self.index_projector.nonzeros)
        else:
            self.entry_value = 1.0

    def __call__(self, state: np.ndarray) -> csr_matrix:
        indices = self.index_projector(state)
        if indices.ndim == 1:
            indices = indices.reshape((1, -1))

        # values for all active features
        vals = np.full(indices.size, self.entry_value, dtype=float)

        # row pointers: assumes uniform nonzero count per row
        row_ptr = np.arange(0, indices.size + 1, indices.shape[1], dtype=int)

        # flatten column indices
        col_ind = indices.flatten().astype(int)

        return csr_matrix((vals, col_ind, row_ptr),
                          shape=(indices.shape[0], self.index_projector.size))

    @property
    def size(self) -> int:
        return self.index_projector.size

    @property
    def xTxNorm(self) -> float:
        return 1.0 if self.normalize else float(self.index_projector.nonzeros)


class IndexToDense(Projector):
    """Wrap an index projector to produce dense 0/entry_value arrays."""
    def __init__(self, index_projector: Projector, normalize: bool = False) -> None:
        super().__init__()
        self.index_projector = index_projector
        self.normalize = bool(normalize)
        if normalize:
            self.entry_value = 1.0 / np.sqrt(self.index_projector.nonzeros)
        else:
            self.entry_value = 1.0

    def __call__(self, state: np.ndarray) -> np.ndarray:
        indices = self.index_projector(state)
        if indices.ndim == 1:
            indices = indices.reshape((1, -1))

        output = np.zeros((indices.shape[0], self.size), dtype=float)
        row_ind = np.tile(np.arange(indices.shape[0]).reshape((-1, 1)),
                          (1, indices.shape[1])).flatten()
        output[row_ind, indices.flatten().astype(int)] = self.entry_value
        return output.squeeze()

    @property
    def size(self) -> int:
        return self.index_projector.size

    @property
    def xTxNorm(self) -> float:
        return 1.0 if self.normalize else float(self.index_projector.nonzeros)


class ConcatProjector(Projector):
    """Concatenate several projectors’ outputs."""
    def __init__(self, projectors: Sequence[Projector]) -> None:
        super().__init__()
        self.phis = list(projectors)

    def __call__(self, state: np.ndarray) -> np.ndarray:
        return np.hstack([phi(state) for phi in self.phis])

    @property
    def size(self) -> int:
        return sum(phi.size for phi in self.phis)


class SparseRPTilecoding(IndexToBinarySparse):
    """Random projection on top of sparse tile coding."""
    def __init__(self, index_projector: Projector, random_proj: sp.spmatrix,
                 normalize: bool = False, output_dense: bool = True) -> None:
        super().__init__(index_projector, normalize=normalize)
        self.random_proj = random_proj
        self.output_dense = bool(output_dense)

    def __call__(self, X: np.ndarray) -> Union[np.ndarray, csr_matrix]:
        phi = super().__call__(X)
        pphi = self.random_proj.dot(phi.T).T
        if self.output_dense:
            pphi = pphi.todense()
        return pphi

    @property
    def size(self) -> int:
        return int(self.random_proj.shape[0])


################## TILE CODING KERNELS #######################################
class DenseKernel(IndexToDense):
    def __call__(self, X1: np.ndarray, X2: Optional[np.ndarray] = None) -> np.ndarray:
        phi1 = super().__call__(X1)
        if X2 is None:
            return phi1.dot(phi1.T)
        phi2 = super().__call__(X2)
        return phi1.dot(phi2.T)


class SparseKernel(IndexToBinarySparse):
    def __call__(self, X1: np.ndarray, X2: Optional[np.ndarray] = None) -> sp.spmatrix:
        phi1 = super().__call__(X1)
        if X2 is None:
            return phi1.dot(phi1.T)
        phi2 = super().__call__(X2)
        return phi1.dot(phi2.T)


################## TILE CODING IMPLEMENTATION ################################
class Tiling(object):
    """
    Represents a series of layers of tile coding (a single discretization
    over selected input dimensions).
    """
    def __init__(self,
                 input_index: Sequence[int],
                 ntiles: Union[int, Sequence[int]],
                 ntilings: int,
                 state_range: np.ndarray,
                 rnd_stream: np.random.RandomState,
                 offset: Optional[np.ndarray] = None,
                 hashing: Optional[Hashing] = None):

        self.hashing = hashing

        if isinstance(ntiles, int):
            ntiles = np.array([ntiles] * len(input_index), dtype=int)
        else:
            ntiles = np.array(ntiles, dtype=int)

        self.state_range = [
            state_range[0][input_index].copy().astype(float)[None, :, None],
            state_range[1][input_index].copy().astype(float)[None, :, None],
        ]

        if ntiles.ndim > 1:
            ntiles = ntiles[None, :, :]
        else:
            ntiles = ntiles[None, :, None]

        # Expand min range slightly to allow negative fractional offsets
        self.state_range[0] = self.state_range[0] - (self.state_range[1] - self.state_range[0]) / (ntiles - 1)

        self.offset = offset
        if offset is None:
            # Shape: (dims, ntilings)
            self.offset = np.empty((ntiles.shape[1], int(ntilings)), dtype=float)
            for i in range(ntiles.shape[1]):
                # Negative random offset in (-1/ntiles[d], 0]
                self.offset[i, :] = -rnd_stream.random_sample(int(ntilings)) / float(ntiles[0, i])

        if self.hashing is None:
            self.hashing = IdentityHash(ntiles)

        self.input_index = np.array(input_index, dtype=int)
        self.size = int(ntilings) * int(self.hashing.memory)
        self.index_offset = (self.hashing.memory * np.arange(int(ntilings))).astype(int)
        self.ntiles = ntiles

    def __call__(self, state: np.ndarray) -> np.ndarray:
        return self.getIndices(state)

    def getIndices(self, state: np.ndarray) -> np.ndarray:
        if state.ndim == 1:
            state = state.reshape((1, -1))[:, :, None]
        else:
            state = state[:, :, None]

        nstate = (state[:, self.input_index, :] - self.state_range[0]) / (self.state_range[1] - self.state_range[0])
        # integer bin indices per tiling layer
        indices = ((self.offset[None, :, :] + nstate) * self.ntiles).astype(int)
        return self.hashing(indices) + self.index_offset[None, :]

    @property
    def ntilings(self) -> int:
        return int(self.offset.shape[1])


class TileCoding(Projector):
    """Full tile coding projector from states to features (indices)."""
    def __init__(self,
                 input_indices: Sequence[Sequence[int]],
                 ntiles: Sequence[Union[int, Sequence[int]]],
                 ntilings: Sequence[int],
                 hashing: Optional[Sequence[Optional[Hashing]]],
                 state_range: Sequence[np.ndarray],
                 rnd_stream: Optional[np.random.RandomState] = None,
                 offsets: Optional[Sequence[Optional[np.ndarray]]] = None,
                 bias_term: bool = True):
        super().__init__()

        # Either supply explicit offsets per tiling set, or a RNG to make them
        if offsets is None and rnd_stream is None:
            raise Exception("Either offsets for each tiling or a random stream (numpy) must be provided.")

        if hashing is None:
            hashing = [None] * len(ntilings)
        if offsets is None:
            offsets = [None] * len(input_indices)

        self.state_range = np.array(state_range, dtype=object)

        # Build each tiling set
        self.tilings = [
            Tiling(in_index, nt, t, self.state_range, rnd_stream, offset=o, hashing=h)
            for in_index, nt, t, h, o in zip(input_indices, ntiles, ntilings, hashing, offsets)
        ]

        # Per-set sizes (each set contributes ntilings[i] columns; each column has hashing.memory bins)
        sizes = [t.size for t in self.tilings]                       # size of each tiling set
        self._size_pre_bias = int(sum(sizes))                        # features before bias
        self.bias_term = bool(bias_term)

        # Base offset per *tiling set* in the concatenated feature space
        base_offsets = np.zeros(len(ntilings), dtype=int)
        if len(self.tilings) > 1:
            base_offsets[1:] = np.cumsum(sizes[:-1], dtype=int)

        # Expand: repeat each base offset by its *tiling count* (no bias here!)
        self.index_offset = np.hstack([
            np.full(tcount, off, dtype=int) for off, tcount in zip(base_offsets, ntilings)
        ])  # shape = (sum(ntilings),)

        # Final size (append a single bias column if requested)
        self.__size = self._size_pre_bias + (1 if self.bias_term else 0)
        self._bias_index = self._size_pre_bias  # remember the id of the bias column

    def __call__(self, state: np.ndarray) -> np.ndarray:
        # Accept (D,) or (N, D)
        if state.ndim == 1:
            state = state.reshape((1, -1))

        # Concatenate indices from every tiling set (columns) → shape (N, sum(ntilings))
        indices = np.hstack([t(state) for t in self.tilings]).astype(int)

        # Shift each column block by its global offset
        indices = indices + self.index_offset[None, :]

        # Append a single bias column at the end, if enabled
        if self.bias_term:
            bias_col = np.full((indices.shape[0], 1), self._bias_index, dtype=int)
            indices = np.hstack((indices, bias_col))

        # Return (sum(ntilings) [+1]) for a single row, else (N, …)
        return indices.squeeze()



    @property
    def size(self) -> int:
        return self.__size

    @property
    def nonzeros(self) -> int:
        return int(sum(t.ntilings for t in self.tilings) + (1 if self.bias_term else 0))


class UNH(Hashing):
    """Universal hashing (UNH) scheme; constants from rlpark implementation."""
    increment = 470

    def __init__(self, memory: int, rnd_stream: np.random.RandomState) -> None:
        super().__init__()
        self.rndseq = np.zeros(16384, dtype=int)
        self.memory = int(memory)
        # Build 16-bit chunks into 32-bit ints
        for _ in range(4):
            # randint is inclusive of low/high here
            chunk = rnd_stream.randint(np.iinfo('int16').min, np.iinfo('int16').max + 1, 16384, dtype=int) & 0xFF
            self.rndseq = (self.rndseq << 8) | chunk

    def __call__(self, indices: np.ndarray) -> np.ndarray:
        rnd_seq = self.rndseq
        a = self.increment * np.arange(indices.shape[1], dtype=int)
        index = indices + a[None, :, None]

        # modulo table size without deprecated astype/float divisions
        index = index % rnd_seq.size

        hashed_index = np.sum(rnd_seq[index], axis=1).astype(int)
        return (hashed_index % self.memory).astype(int)


class IdentityHash(Hashing):
    """Identity/cartesian-product hashing across dimensions."""
    def __init__(self, dims: np.ndarray, wrap: bool = False) -> None:
        super().__init__()
        self.memory = int(np.prod(dims))
        self.dims = dims.astype(int)
        self.wrap = bool(wrap)
        # Precompute dimension multipliers for row-major linearization
        # dims shape is (1, D, 1); build cumulative products accordingly
        dims_vec = self.dims[0, :, 0]
        mul = np.cumprod(np.hstack(([1], dims_vec[::-1][:-1])))[::-1]
        self.dim_offset = mul[None, None, :]

    def __call__(self, indices: np.ndarray) -> np.ndarray:
        if self.wrap:
            idx = np.remainder(indices, self.dims)
        else:
            idx = np.clip(indices, 0, self.dims - 1)
        # idx should be (batch, n_tilings, n_dims); dim_offset is (1,1,n_dims)
        if idx.ndim == 3 and idx.shape[-1] != self.dim_offset.shape[-1]:
            # If idx is (batch, dims, n_tilings), transpose to (batch, n_tilings, dims)
            idx = np.transpose(idx, (0, 2, 1))

        return np.sum(idx * self.dim_offset, axis=2).astype(int)

################## RBF IMPLEMENTATION ########################################
class RBFCoding(Projector):
    """Radial-basis-function feature map with optional normalization and bias."""
    def __init__(self,
                 widths: np.ndarray,
                 centers: np.ndarray,
                 normalized: bool = False,
                 bias_term: bool = True,
                 **params) -> None:
        super().__init__()
        self.c = centers.T[None, :, :]  # (1, D, K)
        if widths.ndim == 1:
            self.w = widths[None, :, None]
        else:
            self.w = widths.T[None, :, :]

        self.normalized = bool(normalized)
        self.bias_term = bool(bias_term)

        self.__size = int(centers.shape[0])
        if self.bias_term:
            self.__size += 1

    def __call__(self, state: np.ndarray) -> np.ndarray:
        if state.ndim == 1:
            state = state.reshape((1, -1))

        last_index = self.size
        output = np.empty((state.shape[0], self.size), dtype=float)
        if self.bias_term:
            last_index -= 1
            output[:, -1] = 1.0

        dsqr = -(((state[:, :, None] - self.c) / self.w) ** 2).sum(axis=1)
        if self.normalized:
            e_x = np.exp(dsqr - dsqr.min(axis=1)[:, None])
            output[:, :last_index] = e_x / e_x.sum(axis=1)[:, None]
        else:
            output[:, :last_index] = np.exp(dsqr)

        return output.squeeze()

    @property
    def size(self) -> int:
        return self.__size


def grid_of_points(state_range: Sequence[np.ndarray], num_centers: Union[int, Sequence[int]]) -> np.ndarray:
    """
    Generate a Cartesian grid of points within state_range for RBF centers.
    """
    if isinstance(num_centers, int):
        num_centers = [num_centers] * state_range[0].shape[0]

    pts_per_dim = [
        np.linspace(start, stop, num, endpoint=True)
        for start, stop, num in zip(state_range[0], state_range[1], num_centers)
    ]
    mesh = np.meshgrid(*pts_per_dim)
    points = np.concatenate([p.reshape((-1, 1)) for p in mesh], axis=1)
    return points


################## END OF RBF IMPLEMENTATION ##################################
