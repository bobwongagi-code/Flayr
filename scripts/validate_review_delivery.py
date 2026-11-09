"""Read-only preflight of a self-contained HTML review artifact, not send approval."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import html
import io
import json
import re
import warnings
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

if __package__:
    from .review_evidence import sha256
    from .validate_learning_review import validate_author_copy
else:
    from review_evidence import sha256
    from validate_learning_review import validate_author_copy

VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
BLOCK = {
    "article",
    "aside",
    "blockquote",
    "br",
    "dd",
    "div",
    "dl",
    "dt",
    "figcaption",
    "figure",
    "footer",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hr",
    "li",
    "main",
    "nav",
    "ol",
    "p",
    "pre",
    "section",
    "table",
    "td",
    "th",
    "tr",
    "ul",
}
RESOURCES = {
    "src",
    "href",
    "xlink:href",
    "poster",
    "data",
    "action",
    "formaction",
    "background",
    "ping",
}
LOCAL_PATH = re.compile(
    r"file\s*:|/(?:Users|home|Volumes|private/var|var/folders|tmp)/|[A-Za-z]:[\\/]",
    re.IGNORECASE,
)
CSS_RESOURCE = re.compile(
    r"(?:url|(?:-webkit-)?image-set|src)\s*\(|@import|@font-face|\\", re.IGNORECASE
)


def decoded(value):
    for _ in range(8):
        current = html.unescape(unquote(value))
        if current == value:
            return value
        value = current
    raise ValueError("Excessively encoded text/resource")


def image_info(uri):
    header, separator, payload = uri.partition(",")
    if not separator or header.lower() not in {
        "data:image/png;base64",
        "data:image/jpeg;base64",
        "data:image/webp;base64",
    }:
        raise ValueError("Images must be embedded base64 PNG/JPEG/WebP")
    raw = base64.b64decode(payload, validate=True)
    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                expected = {
                    "data:image/png;base64": "PNG",
                    "data:image/jpeg;base64": "JPEG",
                    "data:image/webp;base64": "WEBP",
                }[header.lower()]
                if image.format != expected:
                    raise ValueError("Embedded MIME does not match image bytes")
                image.verify()
            with Image.open(io.BytesIO(raw)) as image:
                image.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("Embedded image exceeds Pillow pixel safety limit") from exc
    return len(raw)


class DeliveryHTML(HTMLParser):
    def __init__(self, regions):
        super().__init__(convert_charrefs=True)
        self.regions = {key: [] for key in regions}
        self.region_labels = {key: [] for key in regions}
        self.ids, self.anchors, self.stack = set(), [], []
        self.errors, self.image_count, self.image_bytes = [], 0, 0
        self.inline_scripts = 0
        self.all_text = []
        self.all_labels = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if len(attrs) != len(attributes):
            self.errors.append("Duplicate HTML attributes")
        identity = attrs.get("id")
        if identity is not None:
            if not identity or identity in self.ids:
                self.errors.append(f"Empty or duplicate HTML ID: {identity}")
            self.ids.add(identity)
        if tag in {"base", "iframe", "object", "embed", "form"}:
            self.errors.append(f"Unsupported active element: {tag}")
        if tag == "meta" and (attrs.get("http-equiv") or "").lower() == "refresh":
            self.errors.append("Meta refresh is not portable")
        if tag == "script" and not attrs.get("src"):
            self.inline_scripts += 1
        if "srcset" in attrs or "srcdoc" in attrs:
            self.errors.append("srcset/srcdoc is outside this single-file checker")
        if CSS_RESOURCE.search(attrs.get("style") or ""):
            self.errors.append("CSS resource/escape requires removal from reading copy")
        for key, value in attributes:
            if key not in RESOURCES:
                continue
            if not value:
                self.errors.append(f"Empty resource: {tag}.{key}")
            elif tag == "img" and key == "src":
                try:
                    self.image_bytes += image_info(value)
                    self.image_count += 1
                except (ValueError, OSError, binascii.Error, ImportError) as exc:
                    self.errors.append(f"Invalid embedded image: {exc}")
            elif tag in {"a", "use"} and value.startswith("#"):
                self.anchors.append(unquote(value[1:]))
            else:
                self.errors.append(
                    f"External/local/active resource not allowed: {tag}.{key}"
                )
        if tag == "img" and not attrs.get("src"):
            self.errors.append("Image has no src")
        inherited = self.stack[-1][2] if self.stack else set()
        active = inherited | ({identity} if identity in self.regions else set())
        suppressed = tag in {"script", "style"} or bool(
            self.stack and self.stack[-1][3]
        )
        if not suppressed:
            if tag in BLOCK:
                self.all_text.append("\n")
                for region in active:
                    self.regions[region].append("\n")
            for key in ("alt", "title", "aria-label", "value", "placeholder", "label"):
                if attrs.get(key):
                    self.all_labels.append(attrs[key])
                    for region in active:
                        self.region_labels[region].append(attrs[key])
        if tag not in VOID:
            self.stack.append((tag, identity, active, suppressed))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack or self.stack[-1][0] != tag:
            self.errors.append(f"Unbalanced HTML close tag: {tag}")
            return
        if tag in BLOCK and not self.stack[-1][3]:
            self.all_text.append("\n")
            for region in self.stack[-1][2]:
                self.regions[region].append("\n")
        self.stack.pop()

    def handle_data(self, text):
        if self.stack and self.stack[-1][0] == "style" and CSS_RESOURCE.search(text):
            self.errors.append("CSS resource/escape requires removal from reading copy")
        if not self.stack or not self.stack[-1][3]:
            self.all_text.append(text)
            for region in self.stack[-1][2] if self.stack else ():
                self.regions[region].append(text)


def inspect(path, regions, expected_sha256=None):
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text = raw.decode("utf-8")
    errors = []
    if path.suffix.lower() != ".html":
        errors.append("Only final HTML reading copies are supported")
    if expected_sha256 is not None and digest != expected_sha256:
        errors.append("Artifact changed since the supplied SHA-256")
    if LOCAL_PATH.search(decoded(text)):
        errors.append("Machine path remains in artifact, including internal text")
    parser = DeliveryHTML(regions)
    parser.feed(text)
    parser.close()
    errors.extend(parser.errors)
    if any(
        LOCAL_PATH.search(decoded(part))
        for part in ["".join(parser.all_text), *parser.all_labels]
    ):
        errors.append("Machine path remains in reconstructed artifact text")
    if parser.stack:
        errors.append("Unclosed HTML elements")
    for anchor in parser.anchors:
        if anchor not in parser.ids:
            errors.append(f"Missing fragment target: {anchor}")
    for region, parts in parser.regions.items():
        prose = "".join(parts) + "\n" + "\n".join(parser.region_labels[region])
        if region not in parser.ids or not prose.strip():
            errors.append(f"Missing or empty author region: {region}")
        else:
            # HTML has no Markdown internal-note boundary: scan the entire region.
            for line in prose.splitlines():
                if line.strip() == "## 内部审核备注，不随作者正文发送":
                    errors.append(
                        f"Markdown internal boundary inside author region: {region}"
                    )
            errors.extend(
                f"{region}: {error}"
                for error in validate_author_copy(prose, markdown=False)
            )
    return {
        "schema_version": 1,
        "artifact_name": path.name,
        "artifact_sha256": digest,
        "checker_sha256": sha256(Path(__file__)),
        "author_regions": list(regions),
        "images": parser.image_count,
        "embedded_image_bytes": parser.image_bytes,
        "automatic_check": {
            "status": "failed" if errors else "passed",
            "errors": errors,
        },
        "ai_review": {"status": "not_recorded"},
        "human_confirmation": {"status": "not_recorded"},
        "not_verified": [
            "Screenshot meaning, source truth and recommendation priority",
            "Actual browser layout, script behavior and second physical device",
            "Sharing permission, local-language approval and sending",
        ],
        "inline_scripts_requiring_browser_review": parser.inline_scripts,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path)
    parser.add_argument(
        "--author-region",
        action="append",
        required=True,
        help="ID of an actual author-facing HTML region; repeat as needed",
    )
    parser.add_argument(
        "--expect-sha256", help="Reject a changed previously checked artifact"
    )
    args = parser.parse_args(argv)
    if len(set(args.author_region)) != len(args.author_region) or any(
        not r.strip() for r in args.author_region
    ):
        parser.error("Author region IDs must be nonempty and unique")
    if args.expect_sha256 is not None and not re.fullmatch(
        r"[0-9a-f]{64}", args.expect_sha256
    ):
        parser.error("Expected SHA-256 must be 64 lowercase hex characters")
    try:
        result = inspect(args.artifact, args.author_region, args.expect_sha256)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Cannot inspect final artifact: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["automatic_check"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
