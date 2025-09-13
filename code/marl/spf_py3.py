# spf_py3.py
# Python 3 rewrite of spf.py

from typing import List, Optional


class MarlMachine:
    """
    Not even a state machine, just an interface.
    Action sets state, rather than more complex logic.
    """

    def __init__(
        self,
        values: Optional[List[float]] = None,
        init_state: int = 0,
        ac_space_override: Optional[range] = None,
    ):
        # Avoid mutable default arg
        if values is None:
            values = [float(i) / 10.0 for i in range(10)]

        self._curr_state = init_state
        self._values = values
        self._max_state = len(values) - 1

        # Fix the logic: if user supplies override, use it, else use range(len(values))
        if ac_space_override is not None:
            self.ac_space = ac_space_override
        else:
            self.ac_space = range(len(values))

    def move(self, action: int) -> None:
        if 0 <= action <= self._max_state:
            self._curr_state = action

    def action(self) -> float:
        return self._values[self._curr_state]


class SpfMachine(MarlMachine):
    """
    Basically a state machine seeing whether
    encoding known info about how flows behave
    with an RL agent aids performance.
    Basically a defcon monitor - stay, up, down. [0, 1, 2]
    """

    def __init__(self, values: Optional[List[float]] = None, init_state: int = 1):
        if values is None:
            values = [0.0, 0.05, 0.25, 0.50, 1.0]

        # Pass explicit ac_space_override = range(3)
        super().__init__(values=values, init_state=init_state, ac_space_override=range(3))

    def move(self, action: int) -> None:
        # print(f"in:{self._curr_state} took:{action}")
        if action == 1 and self._curr_state > 0:
            self._curr_state -= 1
        elif action == 2 and self._curr_state < self._max_state:
            self._curr_state += 1
        # print(f"now:{self._curr_state}")
