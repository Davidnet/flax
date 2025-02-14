# Copyright 2024 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Copyright 2023 The Flax Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

import typing as tp
import typing_extensions as tpe

import jax
import jax.tree_util as jtu
import numpy as np

from flax import traverse_util
from flax.nnx.nnx import filterlib, reprlib
from flax.nnx.nnx.variables import VariableState
from flax.typing import Key, PathParts

A = tp.TypeVar('A')

StateLeaf = tp.Union[VariableState[tp.Any], np.ndarray, jax.Array]
FlatState = dict[PathParts, StateLeaf]


def is_state_leaf(x: tp.Any) -> tpe.TypeGuard[StateLeaf]:
  return isinstance(x, (VariableState, np.ndarray, jax.Array))


class NestedStateRepr(reprlib.Representable):
  def __init__(self, state: State):
    self.state = state

  def __nnx_repr__(self):
    yield reprlib.Object('', value_sep=': ', start='{', end='}')

    for r in self.state.__nnx_repr__():
      if isinstance(r, reprlib.Object):
        continue
      yield r


class State(tp.MutableMapping[Key, tp.Any], reprlib.Representable):
  def __init__(
    self,
    mapping: tp.Union[
      tp.Mapping[Key, tp.Mapping | StateLeaf],
      tp.Iterator[tuple[Key, tp.Mapping | StateLeaf]],
    ],
    /,
    *,
    _copy: bool = True,
  ):
    if _copy:
      _mapping = dict(mapping)
    else:
      if not isinstance(mapping, dict):
        raise ValueError(
          'Expected a dictionary when `_copy=False`, '
          f'got {type(mapping)} instead.'
        )
      _mapping = mapping

    if tp.TYPE_CHECKING:
      self._mapping = _mapping
    else:
      super().__setattr__('_mapping', _mapping)

  @property
  def raw_mapping(self) -> tp.Mapping[Key, tp.Mapping[Key, tp.Any] | StateLeaf]:
    return self._mapping  # type: ignore

  def __contains__(self, key) -> bool:
    return key in self._mapping

  def __getitem__(self, key: Key) -> State | StateLeaf:
    value = self._mapping[key]
    if isinstance(value, tp.Mapping):
      return State(value, _copy=False)
    return value

  def __getattr__(self, key: Key) -> State | StateLeaf:
    if '_mapping' not in vars(self) or key not in self._mapping:
      raise AttributeError(f"No attribute '{key}' in State")
    return self[key]

  def __setitem__(self, key: Key, value: State | StateLeaf) -> None:
    if isinstance(value, State):
      self._mapping[key] = value._mapping
    else:
      self._mapping[key] = value

  __setattr__ = __setitem__

  def __delitem__(self, key: Key) -> None:
    del self._mapping[key]

  def __iter__(self) -> tp.Iterator[Key]:
    return iter(self._mapping)

  def __len__(self) -> int:
    return len(self._mapping)

  def __nnx_repr__(self):
    yield reprlib.Object(type(self), value_sep=': ', start='({', end='})')

    for k, v in self.items():
      if isinstance(v, State):
        v = NestedStateRepr(v)
      yield reprlib.Attr(repr(k), v)

  def flat_state(self) -> FlatState:
    return traverse_util.flatten_dict(self._mapping)  # type: ignore

  @classmethod
  def from_flat_path(
    cls, flat_state: tp.Mapping[PathParts, StateLeaf], /
  ) -> State:
    nested_state = traverse_util.unflatten_dict(flat_state)
    return cls(nested_state)

  @tp.overload
  def split(self, first: filterlib.Filter, /) -> 'State': ...

  @tp.overload
  def split(
    self,
    first: filterlib.Filter,
    second: filterlib.Filter,
    /,
    *filters: filterlib.Filter,
  ) -> tuple['State', ...]: ...

  def split(
    self, first: filterlib.Filter, /, *filters: filterlib.Filter
  ) -> tp.Union['State', tuple['State', ...]]:
    filters = (first, *filters)
    *states_, rest = _split_state(self, *filters)

    if rest:
      raise ValueError(
        'Non-exhaustive filters, got a non-empty remainder: '
        f'{rest}.\nUse `...` to match all remaining elements.'
      )

    states: State | tuple[State, ...]
    if len(states_) == 1:
      states = states_[0]
    else:
      states = tuple(states_)
    return states  # type: ignore[bad-return-type]

  @tp.overload
  def filter(
    self,
    first: filterlib.Filter,
    /,
  ) -> 'State': ...

  @tp.overload
  def filter(
    self,
    first: filterlib.Filter,
    second: filterlib.Filter,
    /,
    *filters: filterlib.Filter,
  ) -> tuple['State', ...]: ...

  def filter(
    self,
    first: filterlib.Filter,
    /,
    *filters: filterlib.Filter,
  ) -> tp.Union['State', tuple['State', ...]]:
    *states_, _rest = _split_state(self, first, *filters)

    assert len(states_) == len(filters) + 1

    states: State | tuple[State, ...]
    if len(states_) == 1:
      states = states_[0]
    else:
      states = tuple(states_)

    return states  # type: ignore[bad-return-type]

  @staticmethod
  def merge(state: 'State', /, *states: 'State') -> 'State':
    states = (state, *states)

    if len(states) == 1:
      return states[0]

    new_state: FlatState = {}

    for state in states:
      new_state.update(state.flat_state())  # type: ignore[attribute-error] # pytype is wrong here

    return State.from_flat_path(new_state)

  def __or__(self, other: 'State') -> 'State':
    if not other:
      return self
    return State.merge(self, other)

  def __sub__(self, other: 'State') -> 'State':
    if not other:
      return self

    self_flat = self.flat_state()
    other_flat = other.flat_state()
    diff = {k: v for k, v in self_flat.items() if k not in other_flat}

    return State.from_flat_path(diff)


def _state_flatten_with_keys(x: State):
  items = sorted(x._mapping.items())
  children = tuple((jtu.DictKey(key), value) for key, value in items)
  return children, tuple(key for key, _ in items)


def _state_unflatten(
  static: tuple[Key, ...],
  leaves: tuple[StateLeaf, ...] | tuple[dict[Key, StateLeaf]],
):
  return State(zip(static, leaves))


jax.tree_util.register_pytree_with_keys(
  State,
  _state_flatten_with_keys,
  _state_unflatten,  # type: ignore[arg-type]
)


def _split_state(
  state: State,
  *filters: filterlib.Filter,
) -> tuple[State, ...]:
  for i, filter_ in enumerate(filters):
    if filter_ in (..., True) and i != len(filters) - 1:
      remaining_filters = filters[i + 1 :]
      if not all(f in (..., True) for f in remaining_filters):
        raise ValueError(
          '`...` or `True` can only be used as the last filters, '
          f'got {filter_} it at index {i}.'
        )
  predicates = tuple(map(filterlib.to_predicate, filters))

  flat_state = state.flat_state()

  # we have n + 1 states, where n is the number of predicates
  # the last state is for values that don't match any predicate
  flat_states: tuple[FlatState, ...] = tuple(
    {} for _ in range(len(predicates) + 1)
  )

  for path, value in flat_state.items():
    for i, predicate in enumerate(predicates):
      if predicate(path, value):
        flat_states[i][path] = value  # type: ignore[index] # mypy is wrong here?
        break
    else:
      # if we didn't break, set leaf to last state
      flat_states[-1][path] = value  # type: ignore[index] # mypy is wrong here?

  return tuple(State.from_flat_path(flat_state) for flat_state in flat_states)
