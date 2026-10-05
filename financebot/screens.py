from dataclasses import asdict, dataclass, field
import base64
import json


@dataclass
class Screen:
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)
    document: bytes | None = None
    filename: str = ""
    replace: bool = True
    photo: str | None = None

    def dumps(self):
        data = asdict(self)
        data["document"] = base64.b64encode(self.document).decode() if self.document else None
        return json.dumps(data, ensure_ascii=False)

    @classmethod
    def loads(cls, value):
        data = json.loads(value)
        if data["document"]:
            data["document"] = base64.b64decode(data["document"])
        return cls(**data)


def buttons(items):
    return [[item] for item in items]


def pages(items, page, route, size=8):
    page = max(0, min(page, max(0, (len(items) - 1) // size)))
    result = buttons(items[page * size:(page + 1) * size])
    nav = []
    if page:
        nav.append(("←", f"{route}:{page - 1}"))
    if (page + 1) * size < len(items):
        nav.append(("→", f"{route}:{page + 1}"))
    if nav:
        result.append(nav)
    return result
