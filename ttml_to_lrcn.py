#!/usr/bin/env python3
"""Convert Apple Music TTML lyrics to Lyrics Next v2 (LRCN)."""

from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable


TTML = "http://www.w3.org/ns/ttml"
TTM = "http://www.w3.org/ns/ttml#metadata"
ITUNES = "http://music.apple.com/lyric-ttml-internal"
ITUNES_PUBLIC = "http://itunes.apple.com/lyric-ttml-extensions"
XML = "http://www.w3.org/XML/1998/namespace"


class OptionConflict(ValueError):
    """Raised when requested output options cannot form valid LRCN."""


class FormUnavailable(RuntimeError):
    """Raised when the graphical form cannot be opened."""


class FormCancelled(RuntimeError):
    """Raised when the user closes the graphical form without converting."""


def qname(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def valid_line_id(value: str) -> bool:
    if value != value.strip() or value.lower() == "x-bg":
        return False
    if re.fullmatch(r"(?:\d+(?:\.\d*)?|\.\d+)(?:h|m|s|ms|f|t)?", value):
        return False
    if re.fullmatch(r"\d+(?::\d+){1,3}(?:\.\d+)?", value):
        return False
    return not bool(re.search(r"[,\[\]\r\n]", value))


def apple_attr(element: ET.Element, name: str, default: str | None = None) -> str | None:
    """Read an Apple extension attribute from either public or exported TTML."""
    for namespace in (ITUNES, ITUNES_PUBLIC):
        value = element.get(qname(namespace, name))
        if value is not None:
            return value
    # Apple has changed its extension URI over time; retain prefix-independent
    # compatibility without treating unqualified TTML attributes as extensions.
    for key, value in element.attrib.items():
        if key.startswith("{") and local_name(key) == name:
            namespace = key[1:].split("}", 1)[0]
            if "apple.com/lyric-ttml" in namespace or "music.apple.com/lyric-ttml" in namespace:
                return value
    return default


@dataclass(frozen=True)
class TimeContext:
    frame_rate: Decimal = Decimal(30)
    sub_frame_rate: Decimal = Decimal(1)
    tick_rate: Decimal = Decimal(1)


@dataclass(frozen=True)
class ConversionOptions:
    include_header: bool = True
    include_lyrics_marker: bool = True
    include_song_parts: bool = True
    include_line_end: bool = True
    include_agent: bool = True
    include_line_id: bool = True
    include_syllable_end: bool = True
    include_first_syllable_tag: bool = True
    include_background: bool = True
    background_as_line: bool = False
    append_end_marker: bool = False
    timing_tag_style: str = "angle"
    include_attachment_language: bool = True  # Deprecated aggregate switch.
    include_translation_language: bool = True
    include_transliteration_language: bool = True
    embed_attachments: bool = True
    write_translation_track: bool = False
    write_transliteration_track: bool = False
    translation_format: str = "lnt-full"
    translation_output: str = "lrc"
    transliteration_format: str = "both"
    fake_lqe: bool = False
    lqe_format: str = "qrc"
    beat_format: str = "qrc"
    compatibility_format: str = "none"

    def validate(self) -> None:
        translation_formats = {"lnt-full", "lnt-short", "lrc"}
        formats = {"lrcn", "lrc", "both", "none"}
        if self.translation_format not in translation_formats:
            raise ValueError(f"无效的翻译格式：{self.translation_format}")
        if self.translation_output not in {"lrc", "none"}:
            raise ValueError(f"无效的翻译输出：{self.translation_output}")
        if self.transliteration_format not in formats:
            raise ValueError(f"无效的发音格式：{self.transliteration_format}")
        if self.timing_tag_style not in {"angle", "square", "parenthesis"}:
            raise ValueError(f"无效的逐字标签样式：{self.timing_tag_style}")
        if self.lqe_format not in {"qrc", "lys"}:
            raise ValueError(f"无效的 LQE 格式：{self.lqe_format}")
        if self.beat_format not in {"qrc", "lys"}:
            raise ValueError(f"无效的逐拍格式：{self.beat_format}")
        if self.compatibility_format not in {"none", "enhanced", "eslrc", "qrc", "lys", "lqe"}:
            raise ValueError(f"无效的兼容格式：{self.compatibility_format}")
        if has_embedded_attachments(self) and not self.include_lyrics_marker:
            raise OptionConflict("内嵌翻译或发音时必须保留主歌词格式声明")
        if not self.include_line_id and (
            self.translation_format in {"lnt-full", "lnt-short"}
            or self.transliteration_format in {"lrcn", "both"}
        ):
            raise OptionConflict("主歌词省略 line 时不允许使用 LRCN Trans 格式")
        if self.translation_format == "lrc" and self.transliteration_format not in {"lrc", "none"}:
            raise OptionConflict("标签格式为 LRC 时，发音只能逐行输出或不输出")
        if (
            self.append_end_marker
            and not self.fake_lqe
            and self.compatibility_format not in {"qrc", "lys"}
            and not compatibility_extension_eligible(self)
        ):
            raise OptionConflict(
                "启用兼容扩展前，需省略全部主行扩展字段，并将背景人声设为普通行或不输出"
            )


def qrc_syllable_format(options: ConversionOptions) -> str:
    """Return the QRC/Lys dialect selected for parenthesized beat timing."""
    if options.fake_lqe:
        return options.lqe_format
    return options.compatibility_format if options.compatibility_format in {"qrc", "lys"} else options.beat_format


def uses_qrc_syllable_timing(options: ConversionOptions) -> bool:
    """Whether primary lyric beats use QRC-style postfixed millisecond timing."""
    return options.fake_lqe or options.compatibility_format in {"qrc", "lys"} or options.timing_tag_style == "parenthesis"


def uses_lys_syllable_format(options: ConversionOptions) -> bool:
    return uses_qrc_syllable_timing(options) and qrc_syllable_format(options) == "lys"


def force_qrc_syllable_format(options: ConversionOptions) -> ConversionOptions:
    """Normalize the primary lyric fields required by QRC/Lys beat syntax."""
    if options.fake_lqe or not uses_qrc_syllable_timing(options):
        return options
    return replace(
        options,
        include_lyrics_marker=True,
        include_line_end=True,
        include_agent=False,
        include_line_id=False,
        include_syllable_end=True,
        include_first_syllable_tag=True,
        append_end_marker=False,
        timing_tag_style="parenthesis",
        include_background=options.include_background,
        background_as_line=options.include_background,
    )


def apply_compatibility_format(options: ConversionOptions) -> ConversionOptions:
    """Normalize options to the selected enhanced-LRC, ESLRC, QRC or Lys dialect."""
    if options.fake_lqe:
        return force_fake_lqe(options)
    selected = options.compatibility_format
    if selected == "lqe":
        return force_fake_lqe(replace(options, fake_lqe=True))
    if selected == "none":
        return options
    attachment_options = {
        "translation_format": (
            "lrc" if options.translation_format in {"lnt-full", "lnt-short"} else options.translation_format
        ),
        "transliteration_format": (
            "lrc" if options.transliteration_format in {"lrcn", "both"} else options.transliteration_format
        ),
    }
    if selected in {"qrc", "lys"}:
        return force_qrc_syllable_format(
            replace(options, compatibility_format=selected, beat_format=selected, **attachment_options)
        )
    return replace(
        options,
        include_lyrics_marker=True,
        include_line_end=False,
        include_agent=False,
        include_line_id=False,
        include_syllable_end=False,
        include_first_syllable_tag=False,
        include_background=options.include_background,
        background_as_line=options.include_background,
        append_end_marker=True,
        timing_tag_style="angle" if selected == "enhanced" else "square",
        **attachment_options,
    )


@dataclass(frozen=True)
class BackgroundReference:
    line_id: str
    start: str
    end: str


def compatibility_extension_eligible(options: ConversionOptions) -> bool:
    """Whether the trailing-timestamp compatibility extension is valid."""
    return (
        not options.include_line_end
        and not options.include_agent
        and not options.include_line_id
        and not options.include_syllable_end
        and not options.include_first_syllable_tag
        and (not options.include_background or options.background_as_line)
    )


def has_embedded_attachments(options: ConversionOptions) -> bool:
    return options.embed_attachments and (
        options.translation_output != "none" or options.transliteration_format != "none"
    )


def output_suffix(options: ConversionOptions) -> str:
    """Choose the default suffix after all output options are known."""
    if options.fake_lqe:
        return ".lqe"
    if not has_embedded_attachments(options):
        suffixes = {"enhanced": ".lrc", "eslrc": ".lrc", "qrc": ".qrc", "lys": ".lys"}
        if options.compatibility_format in suffixes:
            return suffixes[options.compatibility_format]
        if options.write_translation_track or options.write_transliteration_track:
            return ".lnt"
    has_main_lyrics_options = any(
        (
            options.include_header,
            options.include_lyrics_marker,
            options.include_song_parts,
            options.include_line_end,
            options.include_agent,
            options.include_line_id,
            options.include_syllable_end,
            options.include_first_syllable_tag,
        )
    )
    has_attachments = has_embedded_attachments(options)
    if options.append_end_marker and not has_main_lyrics_options and not has_attachments:
        return ".lrc"
    return ".lrcn"


def default_output_path(input_path: Path, options: ConversionOptions) -> Path:
    return input_path.with_suffix(output_suffix(options))


def force_fake_lqe(options: ConversionOptions) -> ConversionOptions:
    """Apply the fixed Lyricify Quick Export compatibility preset."""
    return replace(
        options,
        include_header=True,
        include_lyrics_marker=True,
        include_song_parts=False,
        include_line_end=True,
        include_agent=False,
        include_line_id=False,
        include_syllable_end=True,
        include_first_syllable_tag=True,
        include_background=options.background_as_line,
        background_as_line=options.background_as_line,
        append_end_marker=False,
        timing_tag_style="parenthesis",
        translation_format="lrc",
        translation_output="lrc",
        transliteration_format=(
            options.transliteration_format
            if options.transliteration_format in {"lrc", "none"}
            else "lrc"
        ),
        fake_lqe=True,
        lqe_format="lys",
        compatibility_format="lqe",
    )


_OFFSET_TIME = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))(h|m|s|ms|f|t)$")


def parse_time(value: str, context: TimeContext) -> Decimal:
    """Parse TTML/SMIL clock and offset time expressions into seconds."""
    value = value.strip()
    offset = _OFFSET_TIME.fullmatch(value)
    if offset:
        number = Decimal(offset.group(1))
        unit = offset.group(2)
        factors = {
            "h": Decimal(3600),
            "m": Decimal(60),
            "s": Decimal(1),
            "ms": Decimal("0.001"),
            "f": Decimal(1) / context.frame_rate,
            "t": Decimal(1) / context.tick_rate,
        }
        return number * factors[unit]

    parts = value.split(":")
    try:
        if len(parts) == 2:
            minutes, seconds = map(Decimal, parts)
            return minutes * 60 + seconds
        if len(parts) == 3:
            hours, minutes, seconds = map(Decimal, parts)
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 4:
            hours, minutes, seconds = map(Decimal, parts[:3])
            frame_parts = parts[3].split(".", 1)
            frames = Decimal(frame_parts[0])
            if len(frame_parts) == 2:
                frames += Decimal(frame_parts[1]) / context.sub_frame_rate
            return hours * 3600 + minutes * 60 + seconds + frames / context.frame_rate
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"不支持的 TTML 时间格式：{value!r}") from exc


def format_time(value: str, context: TimeContext) -> str:
    milliseconds = int(
        (parse_time(value, context) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
    sign = "-" if milliseconds < 0 else ""
    milliseconds = abs(milliseconds)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    if hours:
        return f"{sign}{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    if minutes:
        return f"{sign}{minutes}:{seconds:02d}.{millis:03d}"
    return f"{sign}{seconds}.{millis:03d}"


def time_context(root: ET.Element) -> TimeContext:
    def decimal_attr(name: str, default: str) -> Decimal:
        raw = root.get(qname(TTML + "#parameter", name), default)
        try:
            return Decimal(raw)
        except InvalidOperation as exc:
            raise ValueError(f"无效的 ttp:{name}：{raw!r}") from exc

    frame_rate = decimal_attr("frameRate", "30")
    multiplier = root.get(qname(TTML + "#parameter", "frameRateMultiplier"))
    if multiplier:
        try:
            numerator, denominator = (Decimal(part) for part in multiplier.split())
            frame_rate *= numerator / denominator
        except (InvalidOperation, ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"无效的 ttp:frameRateMultiplier：{multiplier!r}") from exc
    result = TimeContext(
        frame_rate=frame_rate,
        sub_frame_rate=decimal_attr("subFrameRate", "1"),
        tick_rate=decimal_attr("tickRate", "1"),
    )
    if any(value <= 0 for value in (result.frame_rate, result.sub_frame_rate, result.tick_rate)):
        raise ValueError("frameRate、subFrameRate 和 tickRate 必须大于 0")
    return result


def has_role(element: ET.Element, role: str) -> bool:
    values = element.get(qname(TTM, "role"), "").split()
    return role in values


def wrap_timing(value: str, tag_style: str) -> str:
    if tag_style == "angle":
        return f"<{value}>"
    if tag_style == "square":
        return f"[{value}]"
    return f"({value})"


def formatted_duration(begin: str, end: str, context: TimeContext) -> str:
    return format_time(str(parse_time(end, context) - parse_time(begin, context)), context)


def qrc_milliseconds(value: str, context: TimeContext) -> str:
    return str(
        int((parse_time(value, context) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    )


def qrc_duration(begin: str, end: str, context: TimeContext) -> str:
    return str(
        int(
            ((parse_time(end, context) - parse_time(begin, context)) * 1000).quantize(
                Decimal(1), rounding=ROUND_HALF_UP
            )
        )
    )


def format_lrc_time(value: str, context: TimeContext) -> str:
    """Format an LRC timestamp with mandatory leading zeroes."""
    milliseconds = int(
        (parse_time(value, context) * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    )
    minutes, remainder = divmod(milliseconds, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def timed_content(
    container: ET.Element,
    context: TimeContext,
    include_end: bool = True,
    tag_style: str = "angle",
    postfix_timing: bool = False,
    milliseconds: bool = False,
) -> str:
    """Serialize a TTML container while preserving XML text/tail whitespace."""
    pieces: list[str] = []
    if container.text:
        pieces.append(container.text)
    for child in container:
        begin, end = child.get("begin"), child.get("end")
        content = (
            timed_content(child, context, include_end, tag_style, postfix_timing, milliseconds)
            if len(child)
            else (child.text or "")
        )
        label = ""
        if begin:
            label = qrc_milliseconds(begin, context) if milliseconds else format_time(begin, context)
            if end and include_end:
                label += "," + (
                    qrc_duration(begin, end, context) if milliseconds else formatted_duration(begin, end, context)
                    if postfix_timing
                    else format_time(end, context)
                )
            label = wrap_timing(label, tag_style)
        if postfix_timing:
            pieces.extend((content, label))
        else:
            pieces.extend((label, content))
        if child.tail:
            pieces.append(child.tail)
    return "".join(pieces)


def timed_element(
    element: ET.Element,
    context: TimeContext,
    include_end: bool = True,
    tag_style: str = "angle",
    postfix_timing: bool = False,
    milliseconds: bool = False,
) -> str:
    """Serialize an element, including its own timing when present."""
    begin, end = element.get("begin"), element.get("end")
    label = ""
    if begin:
        value = qrc_milliseconds(begin, context) if milliseconds else format_time(begin, context)
        if end and include_end:
            value += "," + (
                qrc_duration(begin, end, context) if milliseconds else formatted_duration(begin, end, context)
                if postfix_timing
                else format_time(end, context)
            )
        label = wrap_timing(value, tag_style)
    content = (
        timed_content(element, context, include_end, tag_style, postfix_timing, milliseconds)
        if len(element)
        else (element.text or "")
    )
    return content + label if postfix_timing else label + content


def trim_xml_formatting(value: str) -> str:
    """Drop pretty-print indentation, but preserve meaningful plain spaces."""
    value = re.sub(r"^[ \t]*(?:\r?\n[ \t]*)+", "", value)
    value = re.sub(r"(?:[ \t]*\r?\n)+[ \t]*$", "", value)
    return value


def split_main_and_background(
    container: ET.Element,
    context: TimeContext,
    include_end: bool = True,
    tag_style: str = "angle",
    postfix_timing: bool = False,
    milliseconds: bool = False,
) -> tuple[str, list[str]]:
    """Serialize main content and extract x-bg spans in document order."""
    main: list[str] = [container.text or ""]
    backgrounds: list[str] = []
    for child in container:
        if has_role(child, "x-bg"):
            backgrounds.append(
                trim_xml_formatting(timed_element(child, context, include_end, tag_style, postfix_timing, milliseconds))
            )
            if child.tail:
                main.append(child.tail)
            continue
        main.append(timed_element(child, context, include_end, tag_style, postfix_timing, milliseconds))
        if child.tail:
            main.append(child.tail)
    return trim_xml_formatting("".join(main)), backgrounds


def main_timed_leaves(container: ET.Element) -> list[ET.Element]:
    leaves: list[ET.Element] = []

    def visit(parent: ET.Element) -> None:
        for child in parent:
            if has_role(child, "x-bg"):
                continue
            if len(child):
                visit(child)
            elif child.get("begin"):
                leaves.append(child)

    visit(container)
    return leaves


def omit_first_syllable_tag(
    content: str,
    container: ET.Element,
    line_start: str,
    context: TimeContext,
    tag_style: str,
) -> str:
    """Omit a safely inferable first beat tag for enhanced-LRC compatibility."""
    leaves = main_timed_leaves(container)
    if len(leaves) < 2:
        return content
    first_direct = next(
        (child for child in container if not has_role(child, "x-bg")), None
    )
    if first_direct is not leaves[0]:
        return content
    first_begin = leaves[0].get("begin")
    if not first_begin or format_time(first_begin, context) != line_start:
        return content
    patterns = {
        "angle": r"^([ \t]*)<[^>]+>",
        "square": r"^([ \t]*)\[[^\]]+\]",
        "parenthesis": r"^([ \t]*)\([^\)]+\)",
    }
    pattern = patterns[tag_style]
    return re.sub(pattern, r"\1", content, count=1)


def omit_first_background_syllable_tags(
    backgrounds: list[str],
    container: ET.Element,
    context: TimeContext,
    tag_style: str,
) -> list[str]:
    """Apply safe first-beat omission independently to each x-bg line."""
    background_nodes = [child for child in container if has_role(child, "x-bg")]
    result: list[str] = []
    for index, content in enumerate(backgrounds):
        if index >= len(background_nodes):
            result.append(content)
            continue
        bounds = timed_bounds(background_nodes[index], context)
        if bounds is None:
            result.append(content)
            continue
        result.append(
            omit_first_syllable_tag(
                content, background_nodes[index], bounds[0], context, tag_style
            )
        )
    return result


def timed_bounds(element: ET.Element, context: TimeContext) -> tuple[str, str] | None:
    starts: list[Decimal] = []
    ends: list[Decimal] = []
    for node in element.iter():
        begin = node.get("begin")
        end = node.get("end")
        if begin:
            starts.append(parse_time(begin, context))
        if end:
            ends.append(parse_time(end, context))
    if not starts:
        return None
    start = min(starts)
    end = max(ends or starts)
    return format_time(str(start), context), format_time(str(end), context)


def build_line_label(
    start: str,
    end: str,
    agent: str,
    line_id: str,
    options: ConversionOptions,
    lys_property: int | None = None,
) -> str:
    if uses_lys_syllable_format(options) and lys_property is not None:
        return f"[{lys_property}]"
    if uses_qrc_syllable_timing(options):
        return "[" + ",".join(value for value in (start, end) if value) + "]"
    fields = [start]
    if options.include_line_id:
        fields.extend(
            [
                end if options.include_line_end else "",
                agent if options.include_agent and agent else "",
                line_id,
            ]
        )
    elif options.include_line_end:
        fields.append(end)
        if options.include_agent and agent:
            fields.append(agent)
    elif options.include_agent and agent:
        fields.extend(["", agent])
    return "[" + ",".join(fields) + "]"


def lys_property(background: bool, left: bool) -> int:
    return (7 if left else 8) if background else (4 if left else 5)


def append_end_marker(content: str, end: str, options: ConversionOptions) -> str:
    if not options.append_end_marker or not end:
        return content
    return content + wrap_timing(end, options.timing_tag_style)


def iter_children_named(parent: ET.Element, name: str) -> Iterable[ET.Element]:
    return (child for child in parent if local_name(child.tag) == name)


def render_attachment(
    kind: str,
    group: ET.Element,
    context: TimeContext,
    line_starts: dict[str, str],
    include_end: bool = True,
    include_background: bool = True,
    include_language: bool = True,
    include_first_syllable_tag: bool = True,
    tag_style: str = "angle",
    background_mode: str = "keep",
    background_refs: dict[str, list[BackgroundReference]] | None = None,
    compact: bool = False,
) -> list[str]:
    language = group.get(qname(XML, "lang"), "")
    lines = [f"[{kind}: format@LRCN Trans]"]
    if language and include_language:
        lines.append(f"[lang:{language}]")
    for text in group.iter():
        if local_name(text.tag) != "text":
            continue
        line_id = text.get("for")
        if not line_id or line_id not in line_starts:
            continue
        main, backgrounds = split_main_and_background(text, context, include_end, tag_style)
        if not include_first_syllable_tag:
            main = omit_first_syllable_tag(
                main, text, line_starts[line_id], context, tag_style
            )
            backgrounds = omit_first_background_syllable_tags(
                backgrounds, text, context, tag_style
            )
        if main:
            label = line_id if compact else f"{line_starts[line_id]},{line_id}"
            lines.append(f"[{label}]{main}")
        if background_mode == "normal":
            references = (background_refs or {}).get(line_id, [])
            for index, background in enumerate(backgrounds):
                if index < len(references) and background:
                    reference = references[index]
                    label = reference.line_id if compact else f"{reference.start},{reference.line_id}"
                    lines.append(f"[{label}]{background}")
        elif include_background:
            lines.extend(f"[x-bg]{background}" for background in backgrounds if background)
    return lines


def plain_element(element: ET.Element) -> str:
    pieces = [element.text or ""]
    for child in element:
        pieces.append(plain_element(child))
        pieces.append(child.tail or "")
    return "".join(pieces)


def split_plain_main_and_background(container: ET.Element) -> tuple[str, list[str]]:
    """Serialize main/background text separately without timing labels."""
    pieces = [container.text or ""]
    backgrounds: list[str] = []
    for child in container:
        if has_role(child, "x-bg"):
            backgrounds.append(trim_xml_formatting(plain_element(child)))
        else:
            pieces.append(plain_element(child))
        pieces.append(child.tail or "")
    return trim_xml_formatting("".join(pieces)), backgrounds


def render_lrc_attachment(
    kind: str,
    group: ET.Element,
    line_starts: dict[str, str],
    include_language: bool = True,
    include_background: bool = True,
    background_mode: str = "keep",
    background_refs: dict[str, list[BackgroundReference]] | None = None,
    fake_lqe: bool = False,
) -> list[str]:
    """Render an attachment as start-time-only LRC."""
    language = group.get(qname(XML, "lang"), "")
    if fake_lqe and kind == "transliteration":
        label = "pronunciation"
    elif fake_lqe and kind == "translate":
        label = "translation"
    else:
        label = kind
    header = f"[{label}: format@LRC"
    if fake_lqe and language and include_language:
        header += f", language@{language}"
    lines = [header + "]"]
    if language and include_language and not fake_lqe:
        lines.append(f"[lang:{language}]")
    for text in group.iter():
        if local_name(text.tag) != "text":
            continue
        line_id = text.get("for")
        if not line_id or line_id not in line_starts:
            continue
        content, backgrounds = split_plain_main_and_background(text)
        if content:
            lines.append(f"[{line_starts[line_id]}]{content}")
        if background_mode == "normal":
            references = (background_refs or {}).get(line_id, [])
            for index, background in enumerate(backgrounds):
                if index < len(references) and background:
                    lines.append(f"[{references[index].start}]{background}")
        elif include_background:
            lines.extend(f"[x-bg]{background}" for background in backgrounds if background)
    return lines


def joined_syllables(container: ET.Element, skip_background: bool = True) -> str:
    """Join non-background leaf syllables with exactly one separating space."""
    syllables: list[str] = []

    def visit(parent: ET.Element) -> None:
        for child in parent:
            if skip_background and has_role(child, "x-bg"):
                continue
            if len(child):
                visit(child)
                continue
            if child.get("begin"):
                text = "".join(child.itertext()).strip()
                if text:
                    syllables.append(text)

    visit(container)
    return " ".join(syllables)


def joined_background_syllables(container: ET.Element) -> list[str]:
    return [
        content
        for child in container
        if has_role(child, "x-bg")
        for content in [joined_syllables(child, skip_background=False)]
        if content
    ]


def render_line_transliteration(
    group: ET.Element,
    line_starts: dict[str, str],
    include_language: bool = True,
    include_background: bool = True,
    background_mode: str = "keep",
    background_refs: dict[str, list[BackgroundReference]] | None = None,
    fake_lqe: bool = False,
) -> list[str]:
    """Generate an LRC line-level pronunciation track from timed syllables."""
    language = group.get(qname(XML, "lang"), "")
    label = "pronunciation" if fake_lqe else "transliteration"
    header = f"[{label}: format@LRC"
    if fake_lqe and language and include_language:
        header += f", language@{language}"
    lines = [header + "]"]
    if language and include_language and not fake_lqe:
        lines.append(f"[lang:{language}]")
    for text in group.iter():
        if local_name(text.tag) != "text":
            continue
        line_id = text.get("for")
        if not line_id or line_id not in line_starts:
            continue
        content = joined_syllables(text)
        if content:
            lines.append(f"[{line_starts[line_id]}]{content}")
        if background_mode == "normal":
            references = (background_refs or {}).get(line_id, [])
            backgrounds = joined_background_syllables(text)
            for index, background in enumerate(backgrounds):
                if index < len(references):
                    lines.append(f"[{references[index].start}]{background}")
        elif include_background:
            lines.extend(
                f"[x-bg]{background}"
                for background in joined_background_syllables(text)
            )
    return lines


def render_external_tracks(root: ET.Element, options: ConversionOptions) -> dict[str, str]:
    """Render requested translation/pronunciation companion LRC files."""
    if not (options.write_translation_track or options.write_transliteration_track):
        return {}
    context = time_context(root)
    body = next((node for node in root.iter() if local_name(node.tag) == "body"), None)
    if body is None:
        raise ValueError("TTML 中缺少 body 元素")
    line_starts: dict[str, str] = {}
    background_refs: dict[str, list[BackgroundReference]] = {}
    generated_id = generated_background_id = 0

    def visit(container: ET.Element) -> None:
        nonlocal generated_id, generated_background_id
        for child in container:
            if local_name(child.tag) == "div":
                visit(child)
                continue
            if local_name(child.tag) != "p":
                continue
            generated_id += 1
            line_id = apple_attr(child, "key", f"L{generated_id}") or f"L{generated_id}"
            begin, end = child.get("begin"), child.get("end")
            if not begin:
                continue
            line_starts.setdefault(line_id, format_lrc_time(begin, context))
            if options.include_background and options.background_as_line:
                for background in (node for node in child if has_role(node, "x-bg")):
                    bounds = timed_bounds(background, context)
                    if bounds is None:
                        continue
                    generated_background_id += 1
                    background_refs.setdefault(line_id, []).append(
                        BackgroundReference(
                            f"B{generated_background_id}",
                            format_lrc_time(bounds[0], context),
                            format_lrc_time(bounds[1], context),
                        )
                    )

    visit(body)
    translations: list[ET.Element] = []
    transliterations: list[ET.Element] = []
    for node in root.iter():
        if local_name(node.tag) == "translations":
            translations.extend(iter_children_named(node, "translation"))
        elif local_name(node.tag) == "transliterations":
            transliterations.extend(iter_children_named(node, "transliteration"))
    mode = "normal" if options.background_as_line else "keep"
    tracks: dict[str, str] = {}
    if options.write_translation_track:
        sections = [render_lrc_attachment("translate", group, line_starts, options.include_translation_language,
                                          options.include_background, mode, background_refs, False)
                    for group in translations]
        text = "\n\n".join("\n".join(section) for section in sections if section)
        if text:
            tracks["_trans.lrc"] = text.rstrip() + "\n"
    if options.write_transliteration_track:
        sections = [render_line_transliteration(group, line_starts, options.include_transliteration_language,
                                                 options.include_background, mode, background_refs, False)
                    for group in transliterations]
        text = "\n\n".join("\n".join(section) for section in sections if section)
        if text:
            tracks["_pron.lrc"] = text.rstrip() + "\n"
    return tracks


def convert(root: ET.Element, options: ConversionOptions | None = None) -> str:
    options = options or ConversionOptions()
    options = apply_compatibility_format(options)
    options.validate()
    context = time_context(root)
    language = root.get(qname(XML, "lang"), "")
    timing = (apple_attr(root, "timing", "line") or "line").lower()
    timing = "word" if timing in {"word", "syllable"} else "line"

    agents = [node for node in root.iter() if local_name(node.tag) == "agent"]
    agent_types = {
        agent_id: node.get("type", "other")
        for node in agents
        if (agent_id := node.get(qname(XML, "id")))
    }
    lines: list[str] = []
    if options.include_header:
        if options.fake_lqe:
            lines.extend(["[Lyricify Quick Export]", "[version:1.0]"])
        else:
            lines.extend(["[Lyrics Next]", "[version:2.0]", f"[timing:{timing}]"])
        if language and not options.fake_lqe:
            lines.append(f"[lang:{language}]")
        if options.include_agent and not options.fake_lqe:
            for agent in agents:
                agent_id = agent.get(qname(XML, "id"))
                if agent_id:
                    lines.append(f"[agent.{agent_id}:{agent.get('type', 'other')}]")

    translations: list[ET.Element] = []
    transliterations: list[ET.Element] = []
    for node in root.iter():
        name = local_name(node.tag)
        if name == "translations":
            translations.extend(iter_children_named(node, "translation"))
        elif name == "transliterations":
            transliterations.extend(iter_children_named(node, "transliteration"))
    if lines:
        lines.append("")
    emit_lyrics_marker = (
        options.fake_lqe
        or has_embedded_attachments(options)
    )
    if emit_lyrics_marker:
        if options.fake_lqe:
            marker = "Lyricify Syllable" if options.lqe_format == "lys" else "QRC"
            lines.append(f"[lyrics: format@{marker}]")
        elif uses_qrc_syllable_timing(options):
            marker = "Lyricify Syllable" if uses_lys_syllable_format(options) else "QRC"
            lines.append(f"[lyrics: format@{marker}]")
        elif options.append_end_marker:
            marker = "Enhanced LRC" if options.timing_tag_style == "angle" else "ESLRC"
            lines.append(f"[lyrics: format@{marker}]")
        else:
            lines.append("[lyrics: format@Lyrics Next]")
    body = next((node for node in root.iter() if local_name(node.tag) == "body"), None)
    if body is None:
        raise ValueError("TTML 中缺少 body 元素")
    generated_id = 0
    generated_background_id = 0
    line_starts: dict[str, str] = {}
    background_refs: dict[str, list[BackgroundReference]] = {}
    seen_line_ids: set[str] = set()
    previous_agent = ""
    previous_lys_agent: str | None = None
    previous_lys_left = True

    def resolve_lys_direction(agent_id: str) -> bool:
        nonlocal previous_lys_agent, previous_lys_left
        agent_type = agent_types.get(agent_id)
        if agent_type == "group":
            return True
        if agent_type == "other":
            return False
        if previous_lys_agent is None:
            left = agent_id != "v2"
        elif agent_id == previous_lys_agent:
            left = previous_lys_left
        else:
            left = not previous_lys_left
        previous_lys_agent = agent_id
        previous_lys_left = left
        return left

    def render_container(container: ET.Element) -> None:
        nonlocal generated_id, generated_background_id, previous_agent
        div = container if local_name(container.tag) == "div" else None
        if div is not None:
            song_part = apple_attr(div, "song-part")
            if song_part and options.include_song_parts:
                lines.append(f"[{song_part}]")
        for child in container:
            name = local_name(child.tag)
            if name == "div":
                render_container(child)
                continue
            if name != "p":
                continue
            paragraph = child
            generated_id += 1
            line_id = apple_attr(paragraph, "key", f"L{generated_id}") or f"L{generated_id}"
            if options.include_line_id:
                if not valid_line_id(line_id):
                    raise ValueError(f"不合法的歌词行 ID：{line_id!r}")
                if line_id in seen_line_ids:
                    raise ValueError(f"重复的歌词行 ID：{line_id!r}")
                seen_line_ids.add(line_id)
            agent = paragraph.get(qname(TTM, "agent"), "")
            if agent:
                previous_agent = agent
            line_left = resolve_lys_direction(agent) if uses_lys_syllable_format(options) else True
            begin, end = paragraph.get("begin"), paragraph.get("end")
            if not begin:
                raise ValueError(f"歌词行 {line_id!r} 缺少 begin")
            attachment_start = format_lrc_time(begin, context) if options.fake_lqe else format_time(begin, context)
            qrc_timing = uses_qrc_syllable_timing(options)
            start = qrc_milliseconds(begin, context) if qrc_timing else attachment_start
            line_starts.setdefault(line_id, attachment_start)
            end_value = (
                qrc_duration(begin, end, context)
                if qrc_timing and end
                else (format_time(end, context) if end else "")
            )
            main, backgrounds = split_main_and_background(
                paragraph,
                context,
                options.include_syllable_end,
                options.timing_tag_style,
                qrc_timing,
                qrc_timing,
            )
            if not options.include_first_syllable_tag:
                main = omit_first_syllable_tag(
                    main, paragraph, start, context, options.timing_tag_style
                )
                backgrounds = omit_first_background_syllable_tags(
                    backgrounds, paragraph, context, options.timing_tag_style
                )
            lines.append(
                build_line_label(
                    start,
                    end_value,
                    agent,
                    line_id,
                    options,
                    lys_property(False, line_left),
                )
                + append_end_marker(main, end_value, options)
            )
            background_nodes = [child for child in paragraph if has_role(child, "x-bg")]
            if options.include_background and options.background_as_line:
                for index, background in enumerate(backgrounds):
                    generated_background_id += 1
                    background_id = f"B{generated_background_id}"
                    while background_id in seen_line_ids:
                        generated_background_id += 1
                        background_id = f"B{generated_background_id}"
                    if options.include_line_id:
                        seen_line_ids.add(background_id)
                    bounds = timed_bounds(background_nodes[index], context)
                    attachment_background_start = (
                        format_lrc_time(bounds[0], context)
                        if options.fake_lqe and bounds
                        else (bounds[0] if bounds else attachment_start)
                    )
                    attachment_background_end = bounds[1] if bounds else end_value
                    background_start, background_end = bounds or (start, end_value)
                    if qrc_timing and bounds:
                        background_start = qrc_milliseconds(bounds[0], context)
                        background_end = qrc_duration(bounds[0], bounds[1], context)
                    background_refs.setdefault(line_id, []).append(
                        BackgroundReference(
                            background_id, attachment_background_start, attachment_background_end
                        )
                    )
                    lines.append(
                        build_line_label(
                            background_start,
                            background_end,
                            agent or previous_agent,
                            background_id,
                            options,
                            lys_property(True, line_left),
                        )
                        + append_end_marker(background, background_end, options)
                    )
            elif options.include_background:
                lines.extend(f"[x-bg]{background}" for background in backgrounds if background)

    render_container(body)

    if options.embed_attachments and options.translation_output != "none" and options.translation_format in {"lnt-full", "lnt-short"}:
        for group in translations:
            lines.extend(
                [""]
                + render_attachment(
                    "translate",
                    group,
                    context,
                    line_starts,
                    options.include_syllable_end,
                    options.include_background,
                    options.include_translation_language,
                    options.include_first_syllable_tag,
                    options.timing_tag_style,
                    "normal" if options.background_as_line else "keep",
                    background_refs,
                    options.translation_format == "lnt-short",
                )
            )
    if options.embed_attachments and options.translation_output != "none" and options.translation_format == "lrc":
        for group in translations:
            lines.extend(
                [""]
                + render_lrc_attachment(
                    "translate",
                    group,
                    line_starts,
                    options.include_translation_language,
                    options.include_background,
                    "normal" if options.background_as_line else "keep",
                    background_refs,
                    options.fake_lqe,
                )
            )
    if options.embed_attachments and options.transliteration_format in {"lrcn", "both"}:
        for group in transliterations:
            lines.extend(
                [""]
                + render_attachment(
                    "transliteration",
                    group,
                    context,
                    line_starts,
                    options.include_syllable_end,
                    options.include_background,
                    options.include_transliteration_language,
                    options.include_first_syllable_tag,
                    options.timing_tag_style,
                    "normal" if options.background_as_line else "keep",
                    background_refs,
                    options.translation_format == "lnt-short",
                )
            )
    if options.embed_attachments and options.transliteration_format in {"lrc", "both"}:
        for group in transliterations:
            lines.extend(
                [""]
                + render_line_transliteration(
                    group,
                    line_starts,
                    options.include_transliteration_language,
                    options.include_background,
                    "normal" if options.background_as_line else "keep",
                    background_refs,
                    options.fake_lqe,
                )
            )
    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将 Apple Music TTML 歌词转换为 Lyrics Next v2（LRCN）"
    )
    parser.add_argument("input", type=Path, nargs="?", help="输入 .ttml 文件（交互模式可省略）")
    parser.add_argument("-o", "--output", type=Path, help="输出 .lrcn 文件（默认同名）")
    parser.add_argument("--stdout", action="store_true", help="输出到标准输出")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--interactive", dest="interactive", action="store_true", help="强制交互询问")
    mode.add_argument(
        "--non-interactive", dest="interactive", action="store_false", help="禁用交互询问"
    )
    parser.set_defaults(interactive=None)
    parser.add_argument(
        "--text-interactive",
        action="store_true",
        help="使用传统终端问答，而不是图形表单",
    )
    parser.add_argument(
        "--fake-lqe",
        action="store_true",
        help="按 Lyricify Quick Export 兼容预设输出 .lqe",
    )
    parser.add_argument(
        "--lqe-format",
        choices=("qrc", "lys"),
        default="qrc",
        help="已弃用；伪装 LQE 固定使用 Lys",
    )
    parser.add_argument(
        "--translation",
        choices=("lnt-full", "lnt-short", "lrc"),
        help="翻译输出格式",
    )
    parser.add_argument(
        "--transliteration",
        choices=("lrcn", "lrc", "both", "none"),
        help="发音输出格式：逐字、逐行、两者或不输出",
    )
    parser.add_argument("--translation-output", choices=("lrc", "none"), default="lrc", help="是否输出翻译")
    parser.add_argument("--no-embed-attachments", dest="embed_attachments", action="store_false", help="不在主歌词中内嵌翻译和发音")
    parser.add_argument("--write-translation-track", action="store_true", help="额外输出 _trans.lrc")
    parser.add_argument("--write-transliteration-track", action="store_true", help="额外输出 _pron.lrc")
    background = parser.add_mutually_exclusive_group()
    background.add_argument(
        "--background-mode",
        choices=("keep", "normal", "omit"),
        help="背景人声输出为 x-bg、普通行或省略",
    )
    background.add_argument(
        "--no-background",
        dest="background_mode",
        action="store_const",
        const="omit",
        help="省略背景人声（兼容旧参数）",
    )
    background.add_argument(
        "--background",
        dest="background_mode",
        action="store_const",
        const="keep",
        help="保留 [x-bg] 背景人声（兼容旧参数）",
    )
    parser.add_argument(
        "--trailing-end-marker",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="省略行 end 时在行尾追加结束时间戳",
    )
    parser.add_argument(
        "--timing-tag-style",
        choices=("angle", "square", "parenthesis"),
        help="逐字/逐拍与行尾时间戳统一使用 <>、[] 或 ()",
    )
    parser.add_argument(
        "--compatibility-format",
        choices=("enhanced", "eslrc", "qrc", "lys", "lqe"),
        help="自动调整主歌词为增强 LRC、ESLRC、QRC、LYS 或 LQE 格式",
    )
    boolean_options = (
        ("metadata", "保留顶部元数据"),
        ("lyrics-marker", "保留主歌词格式声明"),
        ("song-part", "保留歌曲结构"),
        ("line-end", "保留歌词行结束时间"),
        ("agent", "保留演唱者 ID"),
        ("line-id", "保留歌词行 ID"),
        ("syllable-end", "保留逐字结束时间"),
        ("first-syllable-tag", "保留每行首个节拍标签"),
        ("attachment-language", "保留翻译和发音区段语言"),
        ("translation-language", "翻译区段添加语言信息"),
        ("transliteration-language", "发音区段添加语言信息"),
    )
    for flag, help_text in boolean_options:
        parser.add_argument(
            f"--{flag}",
            action=argparse.BooleanOptionalAction,
            default=None,
            help=help_text,
        )
    return parser.parse_args(argv)


def prompt_yes_no(question: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        print(f"{question} [{hint}]：", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline()
        if answer == "":
            raise ValueError("交互输入已结束")
        answer = answer.strip().lower()
        if not answer:
            return default
        if answer in {"y", "yes", "是", "1"}:
            return True
        if answer in {"n", "no", "否", "0"}:
            return False
        print("请输入 y 或 n。", file=sys.stderr)


def prompt_choice(
    question: str, choices: list[tuple[str, str]], default: str
) -> str:
    print(question, file=sys.stderr)
    for index, (value, label) in enumerate(choices, 1):
        suffix = "（默认）" if value == default else ""
        print(f"  {index}. {label}{suffix}", file=sys.stderr)
    while True:
        print("请选择编号：", end="", file=sys.stderr, flush=True)
        answer = sys.stdin.readline()
        if answer == "":
            raise ValueError("交互输入已结束")
        answer = answer.strip()
        if not answer:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1][0]
        for value, _ in choices:
            if answer.lower() == value:
                return value
        print("请输入有效的编号。", file=sys.stderr)


def option_value(value: bool | None, default: bool) -> bool:
    return default if value is None else value


def build_options(args: argparse.Namespace, interactive: bool) -> ConversionOptions:
    def ask_bool(attribute: str, question: str, default: bool = True) -> bool:
        value = getattr(args, attribute)
        return prompt_yes_no(question, default) if interactive and value is None else option_value(value, default)

    include_header = ask_bool("metadata", "保留顶部 Lyrics Next 元数据吗？")
    include_lyrics_marker = ask_bool(
        "lyrics_marker", "保留 [lyrics: format@Lyrics Next] 声明吗？"
    )
    include_song_parts = ask_bool("song_part", "保留 [Verse]、[Chorus] 等歌曲结构吗？")
    include_line_end = ask_bool("line_end", "保留主歌词行的 end 时间吗？")
    include_agent = ask_bool("agent", "保留主歌词行的 agent 吗？")
    include_line_id = ask_bool("line_id", "保留主歌词行的 line ID 吗？")

    if interactive and getattr(args, "background_mode", None) is None:
        background_mode = prompt_choice(
            "背景人声如何输出？",
            [
                ("keep", "保留 [x-bg] 背景人声"),
                ("normal", "改为普通歌词行"),
                ("omit", "不输出"),
            ],
            "keep",
        )
    else:
        background_mode = getattr(args, "background_mode", None) or "keep"

    if interactive and args.translation is None:
        translation_choices = [("lrc", "LRC"), ("none", "不输出")]
        translation_default = "lrc"
        if include_line_id:
            translation_choices = [
                ("lnt-full", "LNT 完整"),
                ("lnt-short", "LNT 精简"),
                ("lrc", "LRC"),
                ("none", "不输出"),
            ]
            translation_default = "lnt-full"
        translation_format = prompt_choice(
            "翻译使用哪种格式？", translation_choices, translation_default
        )
    else:
        translation_format = args.translation or ("lnt-full" if include_line_id else "lrc")

    if translation_format == "none":
        transliteration_format = "none"
    elif interactive and args.transliteration is None:
        transliteration_choices = [("lrc", "逐行 LRC"), ("none", "不输出")]
        transliteration_default = "lrc"
        if include_line_id and translation_format != "lrc":
            transliteration_choices = [
                ("lrcn", "按节拍划分（如有）"),
                ("lrc", "逐行 LRC"),
                ("both", "节拍（如有）+ 逐行"),
                ("none", "不输出"),
            ]
            transliteration_default = "both"
        transliteration_format = prompt_choice(
            "发音使用哪种格式？", transliteration_choices, transliteration_default
        )
    else:
        transliteration_format = args.transliteration or (
            "lrc" if translation_format == "lrc" or not include_line_id else "both"
        )

    if translation_format == "none":
        if getattr(args, "attachment_language", None) is True:
            raise OptionConflict("不输出附属歌词时不能保留附属歌词语言")
        include_attachment_language = False
    else:
        include_attachment_language = ask_bool(
            "attachment_language", "保留翻译和发音区段的 [lang:...] 吗？"
        )
    include_translation_language = (
        ask_bool("translation_language", "翻译区段添加语言信息吗？")
        if translation_format != "none"
        else option_value(getattr(args, "translation_language", None), True)
    )
    include_transliteration_language = option_value(
        getattr(args, "transliteration_language", None), True
    )

    include_syllable_end = ask_bool("syllable_end", "保留逐字标签中的 end 时间吗？")
    include_first_syllable_tag = ask_bool(
        "first_syllable_tag", "保留每行首个按节拍划分标签吗？"
    )
    preliminary_options = ConversionOptions(
        include_header=include_header,
        include_lyrics_marker=include_lyrics_marker,
        include_song_parts=include_song_parts,
        include_line_end=include_line_end,
        include_agent=include_agent,
        include_line_id=include_line_id,
        include_syllable_end=include_syllable_end,
        include_first_syllable_tag=include_first_syllable_tag,
        include_background=background_mode != "omit",
        background_as_line=background_mode == "normal",
        include_attachment_language=include_attachment_language,
        include_translation_language=include_translation_language,
        include_transliteration_language=include_transliteration_language,
        translation_format=translation_format,
        translation_output=args.translation_output,
        transliteration_format=transliteration_format,
        embed_attachments=args.embed_attachments,
        write_translation_track=args.write_translation_track,
        write_transliteration_track=args.write_transliteration_track,
        lqe_format=args.lqe_format,
    )
    if interactive and args.compatibility_format is None:
        compatibility_format = prompt_choice(
            "使用哪种兼容格式？",
            [
                ("none", "不使用"),
                ("enhanced", "增强 LRC"),
                ("eslrc", "ESLRC"),
                ("qrc", "QRC"),
                ("lys", "LYS"),
                ("lqe", "LQE"),
            ],
            "none",
        )
    else:
        compatibility_format = args.compatibility_format or "none"
    append_end = compatibility_format in {"enhanced", "eslrc"}
    if compatibility_format == "none" and compatibility_extension_eligible(preliminary_options):
        trailing_value = getattr(args, "trailing_end_marker", None)
        append_end = (
            prompt_yes_no("启用兼容扩展并在行尾追加结束时间戳吗？", False)
            if interactive and trailing_value is None
            else option_value(trailing_value, False)
        )
    if append_end and compatibility_format == "none" and interactive and getattr(args, "timing_tag_style", None) is None:
        timing_tag_style = prompt_choice(
            "逐字/逐拍与行尾时间戳使用哪种标签？",
            [("angle", "<>"), ("square", "[]"), ("parenthesis", "()")],
            "angle",
        )
    else:
        timing_tag_style = getattr(args, "timing_tag_style", None) or "angle"
    options = ConversionOptions(
        include_header=include_header,
        include_lyrics_marker=include_lyrics_marker,
        include_song_parts=include_song_parts,
        include_line_end=include_line_end,
        include_agent=include_agent,
        include_line_id=include_line_id,
        include_syllable_end=include_syllable_end,
        include_first_syllable_tag=include_first_syllable_tag,
        include_background=background_mode != "omit",
        background_as_line=background_mode == "normal",
        append_end_marker=append_end,
        timing_tag_style=timing_tag_style,
        include_attachment_language=include_attachment_language,
        include_translation_language=include_translation_language,
        include_transliteration_language=include_transliteration_language,
        translation_format=translation_format,
        translation_output=args.translation_output,
        transliteration_format=transliteration_format,
        embed_attachments=args.embed_attachments,
        write_translation_track=args.write_translation_track,
        write_transliteration_track=args.write_transliteration_track,
        lqe_format=args.lqe_format,
        beat_format=(args.compatibility_format if args.compatibility_format in {"qrc", "lys"} else "qrc"),
        compatibility_format=(compatibility_format or ("qrc" if timing_tag_style == "parenthesis" else "none")),
    )
    options = apply_compatibility_format(options if not args.fake_lqe else replace(options, fake_lqe=True))
    options.validate()
    return options


def build_form_options(
    args: argparse.Namespace, default_output: Path | None, input_path: Path | None
) -> None:
    """Collect conversion settings through a keyboard/mouse-friendly Tk form."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except ImportError as exc:
        raise FormUnavailable(str(exc)) from exc

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise FormUnavailable(str(exc)) from exc
    root.title("TTML 转 LRCN")
    root.resizable(False, False)

    def initial(name: str, default: bool = True) -> bool:
        value = getattr(args, name)
        return default if value is None else value

    metadata = tk.BooleanVar(value=initial("metadata"))
    lyrics_marker = tk.BooleanVar(value=initial("lyrics_marker"))
    song_part = tk.BooleanVar(value=initial("song_part"))
    line_end = tk.BooleanVar(value=initial("line_end"))
    agent = tk.BooleanVar(value=initial("agent"))
    line_id = tk.BooleanVar(value=initial("line_id"))
    syllable_end = tk.BooleanVar(value=initial("syllable_end"))
    first_syllable = tk.BooleanVar(value=initial("first_syllable_tag"))
    attachment_language = tk.BooleanVar(value=initial("attachment_language"))
    translation_language = tk.BooleanVar(value=initial("translation_language"))
    background = tk.StringVar(value=getattr(args, "background_mode", None) or "keep")
    translation = tk.StringVar(
        value=args.translation or "lnt-full"
    )
    transliteration = tk.StringVar(
        value=args.transliteration or "both"
    )
    trailing = tk.BooleanVar(value=bool(getattr(args, "trailing_end_marker", False)))
    tag_style = tk.StringVar(value=getattr(args, "timing_tag_style", None) or "angle")
    compatibility_format = tk.StringVar(
        value=("lqe" if getattr(args, "fake_lqe", False) else getattr(args, "compatibility_format", None))
        or ("qrc" if tag_style.get() == "parenthesis" else "none")
    )
    embed_attachments = tk.BooleanVar(value=True)
    write_translation_track = tk.BooleanVar(value=False)
    write_transliteration_track = tk.BooleanVar(value=False)
    translation_output = tk.StringVar(value="lrc")
    transliteration_language = tk.BooleanVar(value=True)
    output_path = tk.StringVar(value=str(default_output) if default_output else "")
    input_value = tk.StringVar(value=str(input_path) if input_path else "")

    outer = ttk.Frame(root, padding=12)
    outer.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    outer.columnconfigure(0, weight=1)

    input_box = ttk.LabelFrame(outer, text="输入 TTML 文件", padding=8)
    input_box.grid(row=0, column=0, sticky="ew", pady=(0, 8))
    input_box.columnconfigure(0, weight=1)
    input_entry = ttk.Entry(input_box, textvariable=input_value, width=70)
    input_entry.grid(row=0, column=0, sticky="ew")

    def choose_input() -> None:
        path = filedialog.askopenfilename(
            parent=root,
            title="选择 Apple Music TTML 歌词文件",
            filetypes=(("TTML 文件", "*.ttml"), ("XML 文件", "*.xml"), ("所有文件", "*.*")),
        )
        if path:
            input_value.set(path)

    ttk.Button(input_box, text="浏览…", command=choose_input).grid(row=0, column=1, padx=(8, 0))

    lyrics_box = ttk.LabelFrame(outer, text="主歌词", padding=8)
    lyrics_box.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    lyrics_controls: list[ttk.Checkbutton] = []
    for index, (label, variable) in enumerate(
        (
            ("保留顶部元数据", metadata),
            ("保留歌曲结构", song_part),
            ("保留行 end", line_end),
            ("保留 agent", agent),
            ("保留 line ID", line_id),
            ("保留逐字 end", syllable_end),
            ("保留首个节拍标签", first_syllable),
        )
    ):
        control = ttk.Checkbutton(lyrics_box, text=label, variable=variable)
        control.grid(
            row=index // 4, column=index % 4, sticky="w", padx=(0, 12), pady=2
        )
        lyrics_controls.append(control)

    background_box = ttk.LabelFrame(outer, text="背景人声", padding=8)
    background_box.grid(row=2, column=0, sticky="ew", pady=(0, 8))
    background_buttons: list[ttk.Radiobutton] = []
    for column, (value, label) in enumerate(
        (("keep", "保留 [x-bg]"), ("normal", "改为普通行"), ("omit", "不输出"))
    ):
        button = ttk.Radiobutton(background_box, text=label, variable=background, value=value)
        button.grid(
            row=0, column=column, sticky="w", padx=(0, 14)
        )
        background_buttons.append(button)

    attachment_box = ttk.LabelFrame(outer, text="翻译与发音", padding=8)
    attachment_box.grid(row=3, column=0, sticky="ew", pady=(0, 8))
    ttk.Label(attachment_box, text="标签格式（翻译和发音）：").grid(row=0, column=0, sticky="w")
    translation_buttons: list[ttk.Radiobutton] = []
    for column, (value, label) in enumerate(
        (("lnt-full", "LNT 完整"), ("lnt-short", "LNT 精简"), ("lrc", "LRC")),
        1,
    ):
        button = ttk.Radiobutton(attachment_box, text=label, variable=translation, value=value)
        button.grid(row=0, column=column, sticky="w", padx=(0, 10))
        translation_buttons.append(button)
    ttk.Label(attachment_box, text="翻译输出：").grid(row=1, column=0, sticky="w", pady=(5, 0))
    ttk.Radiobutton(attachment_box, text="逐行 LRC", variable=translation_output, value="lrc").grid(row=1, column=1, sticky="w", pady=(5, 0))
    ttk.Radiobutton(attachment_box, text="不输出", variable=translation_output, value="none").grid(row=1, column=2, sticky="w", pady=(5, 0))
    ttk.Label(attachment_box, text="发音输出选项：").grid(row=2, column=0, sticky="w", pady=(5, 0))
    transliteration_buttons: list[ttk.Radiobutton] = []
    for column, (value, label) in enumerate(
        (("lrcn", "按节拍划分（如有）"), ("both", "节拍（如有）+逐行"), ("lrc", "逐行"), ("none", "不输出")),
        1,
    ):
        button = ttk.Radiobutton(attachment_box, text=label, variable=transliteration, value=value)
        button.grid(row=2, column=column, sticky="w", padx=(0, 10), pady=(5, 0))
        transliteration_buttons.append(button)
    attachment_language_check = ttk.Checkbutton(
        attachment_box,
        text="翻译添加语言信息",
        variable=translation_language,
    )
    attachment_language_check.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
    transliteration_language_check = ttk.Checkbutton(
        attachment_box,
        text="发音添加语言信息",
        variable=transliteration_language,
    )
    transliteration_language_check.grid(row=3, column=2, columnspan=2, sticky="w", pady=(6, 0))
    ttk.Checkbutton(attachment_box, text="内嵌附属歌词", variable=embed_attachments).grid(row=4, column=0, sticky="w", pady=(4, 0))
    ttk.Checkbutton(attachment_box, text="输出翻译文件（_trans.lrc）", variable=write_translation_track).grid(row=4, column=1, columnspan=2, sticky="w", pady=(4, 0))
    ttk.Checkbutton(attachment_box, text="输出音译文件（_pron.lrc）", variable=write_transliteration_track).grid(row=4, column=3, columnspan=2, sticky="w", pady=(4, 0))

    trailing_box = ttk.LabelFrame(outer, text="兼容格式", padding=8)
    trailing_box.grid(row=4, column=0, sticky="ew", pady=(0, 8))
    compatibility_buttons: list[ttk.Radiobutton] = []
    for column, (value, label) in enumerate(
        (("none", "不使用"), ("enhanced", "增强 LRC"), ("eslrc", "ESLRC"), ("qrc", "QRC"), ("lys", "LYS"), ("lqe", "LQE"))
    ):
        button = ttk.Radiobutton(trailing_box, text=label, variable=compatibility_format, value=value)
        button.grid(row=0, column=column, sticky="w", padx=(0, 14))
        compatibility_buttons.append(button)
    compatibility_hint = tk.StringVar()
    ttk.Label(trailing_box, textvariable=compatibility_hint).grid(
        row=1, column=0, columnspan=5, sticky="w", pady=(4, 0)
    )

    output_box = ttk.LabelFrame(outer, text="输出文件", padding=8)
    output_box.grid(row=5, column=0, sticky="ew", pady=(0, 8))
    output_box.columnconfigure(0, weight=1)
    output_entry = ttk.Entry(output_box, textvariable=output_path, width=70)
    output_entry.grid(row=0, column=0, sticky="ew")

    def suggested_output_path() -> Path:
        if output_path.get().strip():
            return Path(output_path.get())
        if input_value.get().strip():
            source = Path(input_value.get())
            suffix = ".lqe" if compatibility_format.get() == "lqe" else {
                "enhanced": ".lrc", "eslrc": ".lrc", "qrc": ".qrc", "lys": ".lys",
            }.get(compatibility_format.get(), ".lrcn")
            return source.with_suffix(suffix)
        return Path("output.lrcn")

    def choose_output(initial: Path | None = None) -> Path | None:
        suggested = initial or suggested_output_path()
        path = filedialog.asksaveasfilename(
            parent=root,
            title="选择输出文件",
            initialdir=str(suggested.parent) if suggested.parent.exists() else None,
            initialfile=suggested.name,
            defaultextension=suggested.suffix or ".lrcn",
            filetypes=(("歌词文件", "*.lrcn *.lrc *.qrc *.lys *.lqe *.lnt"), ("所有文件", "*.*")),
        )
        if path:
            output_path.set(path)
            return Path(path)
        return None

    def resolve_existing_output(path: Path) -> Path | None:
        """Ask how to handle a collision and return the selected writable path."""
        def numbered_path(original: Path) -> Path:
            index = 1
            while True:
                candidate = original.with_name(f"{original.stem} ({index}){original.suffix}")
                if not candidate.exists():
                    return candidate
                index += 1

        while path.exists() and not args.force:
            dialog = tk.Toplevel(root)
            dialog.title("输出文件已存在")
            dialog.resizable(False, False)
            dialog.transient(root)
            dialog.grab_set()
            choice = tk.StringVar(value="cancel")
            ttk.Label(dialog, text=f"{path.name} 已存在。", padding=(14, 12, 14, 4)).grid(row=0, column=0, columnspan=3)
            ttk.Label(dialog, text="请选择处理方式：", padding=(14, 0, 14, 10)).grid(row=1, column=0, columnspan=3, sticky="w")
            def close(value: str) -> None:
                choice.set(value)
                dialog.destroy()
            ttk.Button(dialog, text="覆盖", command=lambda: close("overwrite")).grid(row=2, column=0, padx=(14, 6), pady=(0, 12))
            ttk.Button(dialog, text="重命名…", command=lambda: close("rename")).grid(row=2, column=1, padx=6, pady=(0, 12))
            ttk.Button(dialog, text="取消", command=lambda: close("cancel")).grid(row=2, column=2, padx=(6, 14), pady=(0, 12))
            dialog.protocol("WM_DELETE_WINDOW", lambda: close("cancel"))
            root.wait_window(dialog)
            if choice.get() == "overwrite":
                return path
            if choice.get() == "cancel":
                return None
            path = numbered_path(path)
            output_path.set(str(path))
        return path

    browse_button = ttk.Button(output_box, text="浏览…", command=choose_output)
    browse_button.grid(row=0, column=1, padx=(8, 0))

    def update_state(*_unused: object) -> None:
        selected_compatibility = compatibility_format.get()
        lqe_mode = selected_compatibility == "lqe"
        if lqe_mode:
            for variable, value in (
                (metadata, True), (lyrics_marker, True), (song_part, False),
                (line_end, True), (agent, False), (line_id, False),
                (syllable_end, True), (first_syllable, True),
                (translation, "lrc"),
            ):
                if variable.get() != value:
                    variable.set(value)
            if background.get() == "keep":
                background.set("normal")
        elif selected_compatibility in {"enhanced", "eslrc"}:
            for variable, value in (
                (lyrics_marker, True), (line_end, False), (agent, False),
                (line_id, False), (syllable_end, False), (first_syllable, False),
                (trailing, True),
            ):
                if variable.get() != value:
                    variable.set(value)
            if background.get() == "keep":
                background.set("normal")
            if tag_style.get() != ("angle" if selected_compatibility == "enhanced" else "square"):
                tag_style.set("angle" if selected_compatibility == "enhanced" else "square")
        elif selected_compatibility in {"qrc", "lys"}:
            for variable, value in (
                (lyrics_marker, True), (line_end, True), (agent, False),
                (line_id, False), (syllable_end, True), (first_syllable, True),
                (trailing, False),
            ):
                if variable.get() != value:
                    variable.set(value)
            if background.get() == "keep":
                background.set("normal")
            if tag_style.get() != "parenthesis":
                tag_style.set("parenthesis")
        has_line_id = line_id.get()
        for button in translation_buttons[:2]:
            button.configure(state="normal" if has_line_id else "disabled")
        if not has_line_id and translation.get() in {"lnt-full", "lnt-short"}:
            translation.set("lrc")
        lrc_format = translation.get() == "lrc"
        no_attachments = translation_output.get() == "none" and transliteration.get() == "none"
        for button in transliteration_buttons:
            value = button.cget("value")
            enabled = not no_attachments and (not lrc_format or value in {"lrc", "none"})
            button.configure(state="normal" if enabled else "disabled")
        if (not has_line_id or lrc_format) and transliteration.get() in {"lrcn", "both"}:
            transliteration.set("lrc")
        attachment_language_check.configure(
            state="normal" if translation_output.get() != "none" else "disabled"
        )
        transliteration_language_check.configure(
            state="normal" if transliteration.get() != "none" else "disabled"
        )
        if lqe_mode:
            if trailing.get():
                trailing.set(False)
            if tag_style.get() != "parenthesis":
                tag_style.set("parenthesis")
            for control in lyrics_controls:
                control.configure(state="disabled")
            for index, button in enumerate(background_buttons):
                button.configure(state="disabled" if index == 0 else "normal")
            for button in translation_buttons:
                button.configure(state="disabled")
            for button in transliteration_buttons:
                button.configure(
                    state="normal" if button.cget("value") in {"lrc", "none"} else "disabled"
                )
            for button in compatibility_buttons:
                button.configure(state="normal")
            compatibility_hint.set("LQE：固定使用 LYS 的后置 (开始,持续时间) 标签。")
        else:
            for control in lyrics_controls:
                index = lyrics_controls.index(control)
                control.configure(
                    state="disabled" if selected_compatibility != "none" and index >= 2 else "normal"
                )
            for index, button in enumerate(background_buttons):
                button.configure(
                    state="disabled" if selected_compatibility != "none" and index == 0 else "normal"
                )
            for button in compatibility_buttons:
                button.configure(state="normal")
            compatibility_hint.set({
                "none": "LRCN：保留当前主歌词字段；无内嵌翻译/音译时不写格式声明。",
                "enhanced": "增强 LRC：省略主行扩展，使用 <> 行尾结束时间，输出 .lrc。",
                "eslrc": "ESLRC：省略主行扩展，使用 [] 行尾结束时间，输出 .lrc。",
                "qrc": "QRC：毫秒与后置 (开始,持续时间) 逐拍标签，输出 .qrc。",
                "lys": "LYS：带行属性的毫秒逐拍标签，输出 .lys。",
            }[selected_compatibility])

    line_id.trace_add("write", update_state)
    line_end.trace_add("write", update_state)
    agent.trace_add("write", update_state)
    syllable_end.trace_add("write", update_state)
    first_syllable.trace_add("write", update_state)
    background.trace_add("write", update_state)
    translation.trace_add("write", update_state)
    translation_output.trace_add("write", update_state)
    transliteration.trace_add("write", update_state)
    embed_attachments.trace_add("write", update_state)
    trailing.trace_add("write", update_state)
    compatibility_format.trace_add("write", update_state)
    update_state()

    buttons = ttk.Frame(outer)
    buttons.grid(row=6, column=0, sticky="e")
    status = tk.StringVar(value="")
    ttk.Label(outer, textvariable=status).grid(row=7, column=0, sticky="w")

    def submit(copy_to_clipboard: bool = False) -> None:
        if not input_value.get().strip():
            messagebox.showerror("缺少输入文件", "请选择 TTML 文件。", parent=root)
            return
        selected_input = Path(input_value.get())
        if not selected_input.is_file():
            messagebox.showerror("输入文件无效", "请选择存在的 TTML 文件。", parent=root)
            return
        try:
            options = ConversionOptions(
                include_header=metadata.get(),
                include_lyrics_marker=lyrics_marker.get(),
                include_song_parts=song_part.get(),
                include_line_end=line_end.get(),
                include_agent=agent.get(),
                include_line_id=line_id.get(),
                include_syllable_end=syllable_end.get(),
                include_first_syllable_tag=first_syllable.get(),
                include_background=background.get() != "omit",
                background_as_line=background.get() == "normal",
                append_end_marker=trailing.get(),
                timing_tag_style=tag_style.get(),
                include_attachment_language=attachment_language.get(),
                include_translation_language=translation_language.get(),
                include_transliteration_language=transliteration_language.get(),
                translation_format=translation.get(),
                translation_output=translation_output.get(),
                transliteration_format=transliteration.get(),
                embed_attachments=embed_attachments.get(),
                write_translation_track=write_translation_track.get(),
                write_transliteration_track=write_transliteration_track.get(),
                fake_lqe=compatibility_format.get() == "lqe",
                lqe_format="lys",
                beat_format=compatibility_format.get() if compatibility_format.get() in {"qrc", "lys"} else "qrc",
                compatibility_format=compatibility_format.get(),
            )
            options = apply_compatibility_format(options)
            options.validate()
        except ValueError as exc:
            messagebox.showerror("选项无效", str(exc), parent=root)
            return
        selected_output: Path | None = None
        if not copy_to_clipboard:
            selected_output = Path(output_path.get()) if output_path.get().strip() else default_output_path(selected_input, options)
            if options.fake_lqe:
                selected_output = selected_output.with_suffix(".lqe")
            if selected_output.resolve() == selected_input.resolve():
                messagebox.showerror("输出路径无效", "输出文件不能覆盖输入 TTML 文件。", parent=root)
                return
            selected_output = resolve_existing_output(selected_output)
            if selected_output is None:
                return
        try:
            root_element = ET.parse(selected_input).getroot()
            converted = convert(root_element, options)
            if copy_to_clipboard:
                root.clipboard_clear()
                root.clipboard_append(converted)
                root.update()
                status.set("已写入剪贴板；窗口保持打开，可继续转换。")
            elif args.stdout:
                sys.stdout.write(converted)
                status.set("已输出到标准输出；窗口保持打开，可继续转换。")
            else:
                assert selected_output is not None
                selected_output.write_text(converted, encoding="utf-8", newline="\n")
                written_tracks: list[Path] = []
                for suffix, content in render_external_tracks(root_element, options).items():
                    track_path = selected_output.with_name(selected_output.stem + suffix)
                    track_path.write_text(content, encoding="utf-8", newline="\n")
                    written_tracks.append(track_path)
                track_text = "" if not written_tracks else f"，另生成 {len(written_tracks)} 个附属文件"
                status.set(f"转换完成{track_text}；窗口保持打开，可继续转换。")
        except (ET.ParseError, OSError, ValueError) as exc:
            messagebox.showerror("转换失败", str(exc), parent=root)

    def cancel() -> None:
        root.destroy()

    ttk.Button(buttons, text="取消", command=cancel).grid(row=0, column=0, padx=(0, 8))
    convert_button = ttk.Button(buttons, text="开始转换", command=submit)
    convert_button.grid(row=0, column=1)
    convert_button.bind("<Control-Button-1>", lambda _event: submit(True) or "break")
    root.protocol("WM_DELETE_WINDOW", cancel)
    root.bind("<Return>", lambda _event: submit())
    root.bind("<Escape>", lambda _event: cancel())
    output_entry.focus_set()
    root.mainloop()


def prompt_output_path(default: Path) -> Path:
    print(f"输出文件 [{default}]：", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    if answer == "":
        raise ValueError("交互输入已结束")
    return Path(answer.strip()) if answer.strip() else default


def prompt_input_path() -> Path:
    print("输入 TTML 文件：", end="", file=sys.stderr, flush=True)
    answer = sys.stdin.readline()
    if answer == "":
        raise ValueError("交互输入已结束")
    if not answer.strip():
        raise ValueError("未提供输入 TTML 文件")
    return Path(answer.strip())


def choose_input_file_form() -> Path:
    """Open a native file picker before showing the conversion settings form."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise FormUnavailable(str(exc)) from exc

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        raise FormUnavailable(str(exc)) from exc
    root.withdraw()
    try:
        path = filedialog.askopenfilename(
            parent=root,
            title="选择 Apple Music TTML 歌词文件",
            filetypes=(("TTML 文件", "*.ttml"), ("XML 文件", "*.xml"), ("所有文件", "*.*")),
        )
    finally:
        root.destroy()
    if not path:
        raise FormCancelled()
    return Path(path)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.stdout and args.output:
        print("错误：--stdout 与 --output 不能同时使用", file=sys.stderr)
        return 2
    try:
        interactive = (
            args.interactive
            if args.interactive is not None
            else (sys.stdin.isatty() or args.input is None)
        )
        if args.input is None:
            if not interactive:
                raise OptionConflict("未提供输入文件时不能使用 --non-interactive")
            if args.text_interactive:
                if not sys.stdin.isatty():
                    raise OptionConflict("--text-interactive 需要可交互的终端输入")
                args.input = prompt_input_path()
        if interactive and not args.text_interactive:
            try:
                default_form_output = None if args.stdout else args.output
                build_form_options(args, default_form_output, args.input)
                return 0
            except FormUnavailable as exc:
                if not sys.stdin.isatty():
                    raise OptionConflict(f"图形表单不可用：{exc}") from exc
                print("图形表单不可用，已切换为终端问答。", file=sys.stderr)
                if args.input is None:
                    args.input = prompt_input_path()
                root = ET.parse(args.input).getroot()
                options = build_options(args, True)
        else:
            if interactive and not sys.stdin.isatty():
                raise OptionConflict("--text-interactive 需要可交互的终端输入")
            if args.input is None:
                raise OptionConflict("未提供输入文件")
            root = ET.parse(args.input).getroot()
            options = build_options(args, interactive)
        result = convert(root, options)
        if args.stdout:
            sys.stdout.write(result)
        else:
            default_output = args.output or default_output_path(args.input, options)
            output = prompt_output_path(default_output) if interactive and args.output is None else default_output
            if options.fake_lqe:
                output = output.with_suffix(".lqe")
            if output.resolve() == args.input.resolve():
                raise ValueError("输出文件不能覆盖输入 TTML 文件")
            if output.exists() and not args.force:
                if not interactive or not prompt_yes_no(f"{output} 已存在，是否覆盖？", False):
                    raise ValueError(f"输出文件已存在：{output}（使用 --force 覆盖）")
            output.write_text(result, encoding="utf-8", newline="\n")
            for suffix, content in render_external_tracks(root, options).items():
                output.with_name(output.stem + suffix).write_text(content, encoding="utf-8", newline="\n")
            print(f"已生成：{output}")
    except OptionConflict as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2
    except FormCancelled:
        return 0
    except (ET.ParseError, OSError, ValueError) as exc:
        print(f"转换失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
