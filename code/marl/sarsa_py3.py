# sarsa_py3.py
# Python 3 rewrite of sarsa.py (originally Python 2)
# Mirrors the original functionality and interfaces.

from __future__ import annotations

import numpy as np
import random
import sys
from typing import Iterable, List, Optional, Sequence, Tuple

import tilecoding.representation_py3 as r
from spf_py3 import *  # Assumes MarlMachine etc. are provided here


class SarsaLearner:
    """
    Learning agent powered by Sarsa and Tile Coding, with epsilon-greedy.
    Assumes actions are discretised and states are continuous (tile-coded).
    """

    # Tilings taken from:
    # http://etheses.whiterose.ac.uk/8109/1/phd-thesis-malialis.pdf (Section 4.3)

    def __init__(
        self,
        max_bw: float,
        vec_size: int,
        actions: Sequence,
        epsilon: float = 0.3,
        learn_rate: float = 0.05,
        discount: float = 0.0,
        tile_c: int = 6,
        tilings_c: int = 8,
        default_q: float = 0.0,
        epsilon_falloff: int = 1000,
        break_equal: bool = False,
        extended_mins: Optional[List[float]] = None,
        extended_maxes: Optional[List[float]] = None,
        tc_indices: Optional[List[np.ndarray]] = None,
        trace_decay: float = 0.0,
        trace_threshold: float = 0.0001,
        broken_math: bool = False,
        rescale_alpha: Optional[float] = 1.0,
        increase_fixed_math_alpha_if_narrowed: bool = True,
        always_include_bias: bool = True,
        AcTrans=MarlMachine,
    ):
        if extended_mins is None:
            extended_mins = []
        if extended_maxes is None:
            extended_maxes = []

        # State range per feature group (tile coding input)
        state_range = [
            [0 for _ in range(vec_size)] + extended_mins,
            [max_bw for _ in range(vec_size)] + extended_maxes,
        ]

        tc_indices = tc_indices if tc_indices is not None else [np.arange(vec_size)]
        ntiles = [tile_c for _ in tc_indices]
        ntilings = [tilings_c for _ in tc_indices]
        self.ntilings = ntilings
        self.tiling_set_count = len(tc_indices)

        self.tc = r.TileCoding(
            input_indices=tc_indices,
            # NOTE: if you need non-uniform tiles per group, extend this API.
            ntiles=ntiles,
            ntilings=ntilings,
            hashing=None,
            state_range=state_range,
            rnd_stream=np.random.RandomState(),
        )

        # Learning rate bookkeeping (see fixed-math adjustment below)
        self.single_learn_rate = learn_rate
        if (rescale_alpha is not None) and (not broken_math):
            # +1 for bias tile
            learn_rate = (learn_rate * float(rescale_alpha)) / float(sum(ntilings) + 1)

        self.epsilon = float(epsilon)
        self._curr_epsilon = float(epsilon)
        self.epsilon_falloff = int(epsilon_falloff)

        self.learn_rate = float(learn_rate)
        self.discount = float(discount)

        self.actions = list(actions)
        self.break_equal = bool(break_equal)
        self.values = {}  # dict: tile_id -> np.array(len(actions), dtype=float)
        self.default_q = float(default_q)

        # Dynamics flags (kept from original)
        self._argmax_in_dt = False
        self._wipe_trace_if_not_argmax = False
        self.alpha_mod_fixed_math = bool(increase_fixed_math_alpha_if_narrowed)
        self.always_include_bias = bool(always_include_bias)

        self.trace_decay = float(trace_decay)
        self.trace_threshold = float(trace_threshold)

        self.broken_math = bool(broken_math)  # keep original update variant

        self._step_count = 0

    # ---------- Internal helpers ----------

    def _ensure_state_vals_exist(self, state: Sequence[int]) -> None:
        for tile in state:
            if tile not in self.values:
                self.values[tile] = np.full(len(self.actions), float(self.default_q), dtype=float)

    def _get_state_values(self, state: Sequence[int]) -> List[np.ndarray]:
        self._ensure_state_vals_exist(state)
        return [self.values[tile] for tile in state]

    # NOTE: FIXED 2025-09-17 : clamps invalid indices
    def _update_state_values(
        self,
        state: Sequence[int],
        action: int,
        values: np.ndarray,
        narrowing: Optional[Iterable[int]],
    ) -> None:
        self._ensure_state_vals_exist(state)
        n_tiles = len(state)

        if narrowing is None:
            indices = range(n_tiles)
        else:
            # Keep only indices valid for both `state` and the `values` vector we were given
            try:
                indices = [int(i) for i in narrowing if 0 <= int(i) < n_tiles and int(i) < len(values)]
            except Exception:
                indices = range(n_tiles)
            if not indices:
                indices = range(n_tiles)

        for i in indices:
            tile = state[i]
            self.values[tile][action] = float(values[i])

    # def _update_state_values(
    #     self,
    #     state: Sequence[int],
    #     action: int,
    #     values: np.ndarray,
    #     narrowing: Optional[Iterable[int]],
    # ) -> None:
    #     self._ensure_state_vals_exist(state)
    #     if narrowing is None:
    #         narrowing = range(len(state))

    #     for i in narrowing:
    #         tile = state[i]
    #         value = values[i]
    #         self.values[tile][action] = value

    def _compute_relevant_narrowed_tilings(self, narrowing: Sequence[int]) -> np.ndarray:
        """
        Convert a list of feature-group indices into the list of
        concrete tiling indices they influence. Append bias if enabled.
        """
        out = []
        total = 0
        n_i = 0  # index into `narrowing` (assumed sorted in original)
        for i, length in enumerate(self.ntilings):
            if n_i < len(narrowing) and i == narrowing[n_i]:
                out.append(np.arange(total, total + length))
                n_i += 1
            total += length

        if self.always_include_bias:
            out.append(np.array([total]))

        if len(out) == 0:
            return np.array([], dtype=int)
        return np.hstack(out)

    # ---------- Policy ----------

    # NOTE: FIXED 2025-09-17 : clamps invalid indices
    def select_action(self, state: Sequence[int], narrowing: Optional[Iterable[int]] = None):
        """
        Returns:
        (a_index, per_tiling_vals_for_chosen_action, per_tiling_vals_for_argmax_action, action_vals_sum)
        """
        all_tile_action_vals = self._get_state_values(state)
        n_tiles = len(all_tile_action_vals)

        # Narrowing is a list of *positions* in this state's active-tiles list.
        if narrowing is None:
            use_idx = range(n_tiles)
        else:
            try:
                use_idx = [int(i) for i in narrowing if 0 <= int(i) < n_tiles]
            except Exception:
                use_idx = list(range(n_tiles))
            if not use_idx:
                # Nothing valid—fall back to using all tiles
                use_idx = range(n_tiles)

        action_vals = np.zeros(len(self.actions), dtype=float)
        for tile_index in use_idx:
            action_vals += all_tile_action_vals[tile_index]

        a_index = self.select_action_from_vals(action_vals)

        chosen_vals = np.array([av[a_index] for av in all_tile_action_vals], dtype=float)
        argmax_idx = int(np.argmax(action_vals))
        argmax_vals = np.array([av[argmax_idx] for av in all_tile_action_vals], dtype=float)

        return (a_index, chosen_vals, argmax_vals, action_vals)

    # def select_action(self, state: Sequence[int], narrowing: Optional[Iterable[int]] = None):
    #     """
    #     Returns:
    #       (a_index, per_tiling_vals_for_chosen_action, per_tiling_vals_for_argmax_action, action_vals_sum)
    #     """
    #     all_tile_action_vals = self._get_state_values(state)

    #     if narrowing is None:
    #         narrowing = range(len(all_tile_action_vals))

    #     action_vals = np.zeros(len(self.actions), dtype=float)
    #     for tile_index in narrowing:
    #         action_vals += all_tile_action_vals[tile_index]

    #     a_index = self.select_action_from_vals(action_vals)
    #     return (
    #         a_index,
    #         np.array([av[a_index] for av in all_tile_action_vals], dtype=float),
    #         np.array([av[np.argmax(action_vals)] for av in all_tile_action_vals], dtype=float),
    #         action_vals,
    #     )

    def select_action_from_vals(self, vals: np.ndarray) -> int:
        # Epsilon-greedy (linear-decreasing epsilon handled in decay())
        if np.random.uniform() < self._curr_epsilon:
            return int(np.random.randint(len(self.actions)))
        if self.break_equal:
            candidates = np.flatnonzero(vals == vals.max())
            return int(np.random.choice(candidates))
        return int(np.argmax(vals))

    def get_epsilon(self) -> float:
        return float(self._curr_epsilon)

    # ---------- Interaction API ----------

    # NOTE: caller must tile-code raw observation first via self.tc(...)
    def bootstrap(self, state: Sequence[int]):
        a_index, _, _, ac_values = self.select_action(state)
        z = z_vec(state) if self.trace_decay > 0.0 else None
        self.last_act = (state, a_index, z)
        return (a_index, ac_values, z)

    # Ditto: caller should pass tile-coded `state`
    def update(
        self,
        state: Sequence[int],
        reward: float,
        subs_last_act=None,
        decay: bool = True,
        delta_space: Optional[List[float]] = None,
        action_narrowing: Optional[Sequence[int]] = None,
        update_narrowing: Optional[Sequence[int]] = None,
    ):
        (last_state, last_action, last_z) = (self.last_act if subs_last_act is None else subs_last_act)

        # Snapshot current values for last state/action
        all_tile_action_vals = self._get_state_values(last_state)
        last_values = np.array([av[last_action] for av in all_tile_action_vals], dtype=float)

        if action_narrowing is not None:
            action_narrowing = self._compute_relevant_narrowed_tilings(action_narrowing)
        if update_narrowing is not None:
            update_narrowing = self._compute_relevant_narrowed_tilings(update_narrowing)

        # Choose new action using possibly narrowed view
        (new_action, new_values, argmax_values, ac_values) = self.select_action(state, action_narrowing)

        next_vals = argmax_values if self._argmax_in_dt else new_values
        argmax_chosen = np.all(new_values == argmax_values)

        vec_d_t = self.discount * next_vals - last_values + reward
        scalar_d_t = self.discount * np.sum(next_vals) - np.sum(last_values) + reward

        d_t = vec_d_t if self.broken_math else scalar_d_t

        if delta_space is not None:
            delta_space.append(float(scalar_d_t))
            delta_space += list(np.asarray(vec_d_t, dtype=float))

        # Learning rate; optionally concentrate when updating a narrowed subset
        alpha = self.learn_rate
        if (not self.broken_math) and self.alpha_mod_fixed_math and (update_narrowing is not None):
            alpha = self.single_learn_rate / float(len(update_narrowing))

        ad_t = alpha * d_t

        if last_z is None:
            updated_vals = last_values + ad_t
            self._update_state_values(last_state, last_action, updated_vals, update_narrowing)
            new_z = None
        else:
            (old_indices, old_grads) = last_z
            if self._wipe_trace_if_not_argmax and not argmax_chosen:
                old_grads = np.array([], dtype=float)
                old_indices = tuple([])
            else:
                old_grads = np.asarray(old_grads, dtype=float) * (self.trace_decay * self.discount)

            new_z = merge_z_vec(state, (old_indices, old_grads), self.trace_threshold)

            # Update values at traced tiles for the chosen action
            state_tiles_to_mutate = self._get_state_values(new_z[0])
            action_tile_vals = np.array([av[last_action] for av in state_tiles_to_mutate], dtype=float)
            updated_vals = action_tile_vals + ad_t * new_z[1]
            self._update_state_values(new_z[0], last_action, updated_vals, update_narrowing)
            # print debugging (fixed variable name)
            # print("vals are:", updated_vals, "from:", action_tile_vals, "by:", d_t)

        if decay:
            self.decay()

        self.last_act = (state, new_action, new_z)
        return (new_action, ac_values, new_z)

    def decay(self) -> None:
        self._curr_epsilon = max(
            0.0, (1.0 - self._step_count / float(self.epsilon_falloff)) * float(self.epsilon)
        )
        self._step_count += 1

    def to_state(self, *args) -> Tuple[int, ...]:
        out = self.tc(*args)
        return tuple(out)


class QLearner(SarsaLearner):
    def __init__(self, **args):
        super().__init__(**args)
        self._argmax_in_dt = True
        self._wipe_trace_if_not_argmax = True


def z_vec(index_list: Sequence[int]) -> Tuple[Tuple[int, ...], np.ndarray]:
    return (tuple(index_list), np.ones(len(index_list), dtype=float))


def merge_z_vec(
    s_new: Sequence[int], l_old: Tuple[Tuple[int, ...], np.ndarray], thres: float
) -> Tuple[Tuple[int, ...], np.ndarray]:
    """
    Merge new trace indices (s_new) with old (s_old, z_old), summing duplicate tiles,
    and dropping very small traces (< thres).
    """
    (s_old, z_old) = l_old
    i = 0
    j = 0
    out_s: List[int] = []
    out_z: List[float] = []
    s_last = -1

    while i < len(s_new) or j < len(s_old):
        select_old = j < len(s_old) and (not (i < len(s_new)) or s_old[j] <= s_new[i])
        (s, val) = ((s_old[j], float(z_old[j])) if select_old else (s_new[i], 1.0))

        if s == s_last:
            out_z[-1] += val
        else:
            out_s.append(s)
            out_z.append(val)

        s_last = s
        if select_old:
            j += 1
        else:
            i += 1

    # Drop tiny traces
    k = len(out_s) - 1
    while k >= 0:
        if out_z[k] < thres:
            out_s.pop(k)
            out_z.pop(k)
        k -= 1

    return (tuple(out_s), np.array(out_z, dtype=float))
