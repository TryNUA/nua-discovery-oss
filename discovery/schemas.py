"""Model replies as pydantic models: one class per reply describes both the JSON schema sent to the
model and the lenient reading of what comes back.

`strict_schema` turns a reply model into the strict subset every provider accepts: every object
lists all its properties as required and closes additionalProperties, optional values are nullable
through anyOf, and nothing is referenced ($defs are inlined). That subset works for Anthropic and
Gemini structured output and for OpenAI strict mode.

The field types below never reject a value a model might plausibly send (a turn written "T12", a
null where a list was asked for, a string where a bool was): they coerce it the way the readers
always have, so one odd field never costs a whole reply. Only `RequiredObjects` fails, when the one
list a reply is useless without is not a list.
"""
from __future__ import annotations

import re
from typing import Annotated, Any, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError, WithJsonSchema

from discovery.llm import LLMError

T = TypeVar("T")
M = TypeVar("M", bound=BaseModel)

_ORDER = ("type", "additionalProperties", "required", "properties", "items", "enum", "anyOf")
_DROP = ("title", "default", "description")


class Reply(BaseModel):
    """Base for model replies: keys the schema does not name are ignored."""
    model_config = ConfigDict(extra="ignore")


# ---------- schema ----------

def strict_schema(model: type[BaseModel]) -> dict:
    """The strict JSON schema for `model`: $defs inlined, titles, defaults and descriptions dropped,
    every object closed with all its properties required, keys in a fixed order."""
    raw = model.model_json_schema(mode="validation")
    defs = raw.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
        node = {k: v for k, v in node.items() if k not in _DROP and k != "$defs"}
        if "properties" in node:
            node["properties"] = {name: walk(sub) for name, sub in node["properties"].items()}
            node["required"] = list(node["properties"])
            node["additionalProperties"] = False
        for key in ("items", "anyOf"):
            if key in node:
                node[key] = walk(node[key])
        ordered = {k: node[k] for k in _ORDER if k in node}
        ordered.update({k: v for k, v in node.items() if k not in ordered})
        return ordered

    return walk(raw)


def parse_reply(model: type[M], data: Any, what: str) -> M:
    """`data` read as `model`. Raises LLMError naming `what` when it does not fit."""
    try:
        return model.model_validate(data)
    except ValidationError as e:
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())) or "reply"
        raise LLMError(f"{what} has the wrong shape ({where}: {first.get('msg', 'invalid')})") from None


# ---------- lenient field types ----------

def _text(v: Any) -> str:
    return "" if v is None else str(v)


def _opt_text(v: Any) -> str | None:
    return None if v is None else str(v)


def turn_number(value: Any) -> int | None:
    """12, "12" or "T12" as 12; anything else (a bool included) as None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    m = re.fullmatch(r"\s*[Tt]?(\d+)\s*", str(value)) if value is not None else None
    return int(m.group(1)) if m else None


def _turns(v: Any) -> list[int]:
    return [t for t in (turn_number(x) for x in v) if t is not None] if isinstance(v, list) else []


def _texts(v: Any) -> list[Any]:
    return list(v) if isinstance(v, list) else []


def _objects(v: Any) -> list[Any]:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _required_objects(v: Any) -> Any:
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else v


Text = Annotated[str, BeforeValidator(_text)]                       # None as "", anything else as its text
OptText = Annotated[str | None, BeforeValidator(_opt_text)]         # None kept, anything else as its text
Flag = Annotated[bool, BeforeValidator(bool)]
OptTurn = Annotated[int | None, BeforeValidator(turn_number)]
Turns = Annotated[list[int], BeforeValidator(_turns)]
Texts = Annotated[list[Text], BeforeValidator(_texts)]              # not a list: []
Objects = Annotated[list[T], BeforeValidator(_objects)]             # non-dict items dropped; not a list: []
RequiredObjects = Annotated[list[T], BeforeValidator(_required_objects)]   # not a list: the reply fails
Mapping = Annotated[T, BeforeValidator(lambda v: v if isinstance(v, dict) else {})]
ObjectOrNone = Annotated[T | None, BeforeValidator(lambda v: v if isinstance(v, dict) else None)]


def choice(values: tuple[str, ...] | list[str], *, nullable: bool = False) -> Any:
    """A str limited to `values` in the schema. Any other value reads as None."""
    allowed = tuple(values)
    enum = {"type": "string", "enum": list(allowed)}
    schema = {"anyOf": [enum, {"type": "null"}]} if nullable else enum
    return Annotated[str | None, BeforeValidator(lambda v: v if isinstance(v, str) and v in allowed else None),
                     WithJsonSchema(schema)]
