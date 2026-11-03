#!/usr/bin/env python3
"""Check the buyer guide against the template and the image's exact strings, offline.

Usage: guide-check.py [--guide PATH] [--template PATH] [--app-repo DIR]

Each check prints one line, ok or the reasons it failed; the exit status is 1
if any failed. Nothing here reaches AWS, and only the standard library is used
(the template is read as text, not parsed as YAML).

Without --app-repo the guide is compared with the template: the Region list in
'Pick one supported Region' equals the SupportedRegion rule, in order; the
model table equals LanguageModel's AllowedValues, in order, with the template's
default marked '(default)'; the parameter and output tables name exactly the
template's parameters and outputs; and every in-page link (#anchor) names a
heading. --app-repo DIR names a checkout of the application (ai-employee,
branch aws-marketplace-offer) and adds: every message the guide quotes as the
product's own occurs in the Exact strings table of its docs/aws-marketplace.md.

Which inline code spans are quoted messages: spans outside fenced blocks with
at least two spaces, or starting with '{"', except commands and paths (a span
starting with '/', '#', '-', or a shell command named in COMMAND_PREFIXES). A
span in double quotes is a log filter pattern and must be a substring of one
of the table's strings. A message may fill a table string's placeholders: a
<reason> with another table string, any other placeholder with any text; the
guide may also keep the placeholder itself. A span that is not the product's
message (an AWS or Microsoft message, a file name, a JSON example) must be
listed in NOT_PRODUCT_MESSAGES below, or occur verbatim in the template.
"""
import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GUIDE = ROOT / "docs" / "aws-deployment-guide.md"
DEFAULT_TEMPLATE = ROOT / "cloudformation" / "nlsql-ai-employee-ecs-fargate.yaml"
APP_STRINGS_FILE = Path("docs") / "aws-marketplace.md"
APP_STRINGS_SECTION = "## Exact strings"

REGION = re.compile(r"\b[a-z]{2}-[a-z]+-[0-9]\b")
REGION_PARAGRAPH = "**Pick one supported Region"
MODEL_ROW = re.compile(r"^\| `(global\.anthropic\.[^`]+)`( \(default\))? \|", re.M)
PARAMETER_TABLE_HEADING = "### Every parameter"
OUTPUT_TABLE_HEADER = "| Output | What it is |"

# Spans that look like sentences but are not the product's own messages.
# Each needs a reason; keep this list short.
NOT_PRODUCT_MESSAGES = frozenset({
    "ECS Deployment Circuit Breaker was triggered",   # ECS's stack event
    "did not contain json key",                       # ECS's ResourceInitializationError
    "AI Employee Uploads/GLOBAL_MEMORY.md",           # a OneDrive path
    '{"AppPassword":"<bot-client-secret>"}',          # the Step 1 secret, a JSON example
})
# Inline commands, not messages.
COMMAND_PREFIXES = ("aws ", "az ", "curl ", "git ", "make ", "openssl ", "teams/")
REASON_PLACEHOLDERS = frozenset({"<reason>"})
ELISIONS = frozenset({"...", "…"})
MAX_DEPTH = 4


def squash(text):
    return " ".join(text.split())


def without_fences(text):
    """The text with fenced code blocks blanked, line count kept."""
    out, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            out.append("")
        else:
            out.append("" if fenced else line)
    return "\n".join(out)


def code_spans(text):
    """Inline code spans outside fenced blocks, whitespace squashed."""
    return [squash(m.group(2)) for m in re.finditer(r"(`+)(.+?)\1", without_fences(text), re.S)]


def section(text, heading):
    """From the line `heading` to the next heading of the same or a higher level."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == heading:
            end = len(lines)
            for j in range(i + 1, len(lines)):
                m = re.match(r"^(#+) ", lines[j])
                if m and len(m.group(1)) <= level:
                    end = j
                    break
            return "\n".join(lines[i:end])
    raise ValueError(f"no section {heading!r}")


def first_column(table_text):
    """The first cell's code spans of every body row of the first table in table_text."""
    names, seen_header = [], False
    for line in table_text.split("\n"):
        if not line.startswith("|"):
            if seen_header:
                break
            continue
        if not seen_header:
            seen_header = True
            continue
        cell = line.split("|")[1]
        if set(cell.strip()) <= set("-: "):
            continue
        names += re.findall(r"`([^`]+)`", cell)
    return names


# ---- the template, read as text -------------------------------------------

def top_level_block(template, key):
    m = re.search(rf"^{key}:\n(.*?)(?=^\S|\Z)", template, re.M | re.S)
    if not m:
        raise ValueError(f"the template has no {key} section")
    return m.group(1)


def template_names(template, key):
    return re.findall(r"^  ([A-Za-z0-9]+):\s*$", top_level_block(template, key), re.M)


def template_regions(template):
    rules = top_level_block(template, "Rules")
    m = re.search(r"SupportedRegion:.*?Fn::Contains:\s*-\s*\[(.*?)\]", rules, re.S)
    if not m:
        raise ValueError("the template's SupportedRegion rule has no Region list")
    return REGION.findall(m.group(1))


def template_models(template):
    params = top_level_block(template, "Parameters")
    m = re.search(r"^  LanguageModel:\n(.*?)(?=^  \S|\Z)", params, re.M | re.S)
    if not m:
        raise ValueError("the template has no LanguageModel parameter")
    block = m.group(1)
    default = re.search(r"^    Default: (\S+)\s*$", block, re.M)
    values = re.search(r"^    AllowedValues:\n((?:      - \S+\n)+)", block, re.M)
    if not default or not values:
        raise ValueError("LanguageModel has no Default or no AllowedValues list")
    return default.group(1), re.findall(r"- (\S+)", values.group(1))


# ---- the checks -----------------------------------------------------------

def check_regions(ctx):
    paragraph = next((p for p in ctx["guide"].split("\n\n") if p.startswith(REGION_PARAGRAPH)), None)
    if paragraph is None:
        return [f"no paragraph starting {REGION_PARAGRAPH!r}"]
    # Code spans (the image's registry host) are not part of the list.
    listed = re.sub(r"`[^`]*`", "", paragraph)
    guide, template = REGION.findall(listed), template_regions(ctx["template"])
    if guide != template:
        return [f"the guide lists {len(guide)} Regions {guide}, the template {len(template)} {template}"]
    return []


def check_models(ctx):
    default, values = template_models(ctx["template"])
    rows = MODEL_ROW.findall(ctx["guide"])
    problems = []
    if [model for model, _ in rows] != values:
        problems.append(f"the model table lists {[m for m, _ in rows]}, LanguageModel allows {values}")
    marked = [model for model, flag in rows if flag]
    if marked != [default]:
        problems.append(f"the table marks {marked or 'nothing'} as the default, the template's is {default}")
    return problems


def check_parameters(ctx):
    guide = first_column(section(ctx["guide"], PARAMETER_TABLE_HEADING))
    template = template_names(ctx["template"], "Parameters")
    return compare_names("parameter", guide, template)


def check_outputs(ctx):
    text = ctx["guide"]
    at = text.find(OUTPUT_TABLE_HEADER)
    if at < 0:
        return [f"no table with the header {OUTPUT_TABLE_HEADER!r}"]
    return compare_names("output", first_column(text[at:]), template_names(ctx["template"], "Outputs"))


def compare_names(kind, guide, template):
    problems = []
    if missing := [n for n in template if n not in guide]:
        problems.append(f"the guide's {kind} table lacks {missing}")
    if extra := [n for n in guide if n not in template]:
        problems.append(f"the guide's {kind} table names {extra}, which the template does not have")
    if dupes := sorted({n for n in guide if guide.count(n) > 1}):
        problems.append(f"the guide's {kind} table repeats {dupes}")
    return problems


def slug(heading):
    """GitHub's anchor for a heading."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def check_anchors(ctx):
    text = without_fences(ctx["guide"])
    anchors, counts = set(), {}
    for m in re.finditer(r"^#{1,6} (.+)$", text, re.M):
        base = slug(m.group(1))
        n = counts.get(base, 0)
        counts[base] = n + 1
        anchors.add(base if n == 0 else f"{base}-{n}")
    broken = sorted({a for a in re.findall(r"\]\(#([^)]+)\)", text) if a not in anchors})
    return [f"no heading for #{a}" for a in broken]


def table_strings(app_doc):
    """Every code span in the table rows of the Exact strings section."""
    strings = []
    for line in section(app_doc, APP_STRINGS_SECTION).split("\n"):
        if line.startswith("|") and not set(line) <= set("|-: "):
            strings += [squash(s) for s in re.findall(r"`([^`]+)`", line)]
    return [s for s in strings if s]


def pattern_of(string):
    """A regex for a table string: literal pieces, each <placeholder> a group."""
    pieces = re.split(r"(<[^<>]+>)", string)
    holders = pieces[1::2]
    regex = "".join(re.escape(p) if i % 2 == 0 else "(.+?)" for i, p in enumerate(pieces))
    return re.compile(regex + r"\Z", re.S), holders


def matches(span, strings, depth=0):
    if span in strings:
        return True
    if depth >= MAX_DEPTH:
        return False
    for string in strings:
        regex, holders = pattern_of(string)
        if not holders:
            continue
        m = regex.match(span)
        if not m:
            continue
        ok = True
        for holder, value in zip(holders, m.groups()):
            if holder in REASON_PLACEHOLDERS and not (
                    re.fullmatch(r"<[^<>]+>", value) or value in ELISIONS
                    or matches(value, strings, depth + 1)):
                ok = False
                break
        if ok:
            return True
    return False


def is_quoted_message(span):
    if span.startswith(("/", "#", "-")) or span.startswith(COMMAND_PREFIXES):
        return False
    return span.count(" ") >= 2 or span.startswith('{"')


def check_exact_strings(ctx):
    strings = table_strings(ctx["app_doc"])
    template = squash(ctx["template"])
    problems = []
    for span in dict.fromkeys(code_spans(ctx["guide"])):
        if len(span) > 2 and span.startswith('"') and span.endswith('"') and '"' not in span[1:-1]:
            needle = span[1:-1]
            if not any(needle in s for s in strings):
                problems.append(f"the filter pattern {span} is in no exact string")
            continue
        if not is_quoted_message(span) or span in NOT_PRODUCT_MESSAGES or span in template:
            continue
        if not matches(span, strings):
            problems.append(f"`{span}` is not in the Exact strings table "
                            "(quote it from there, or list it in NOT_PRODUCT_MESSAGES)")
    return problems


CHECKS = [
    ("Regions equal the template's SupportedRegion", check_regions),
    ("model table equals LanguageModel, default marked", check_models),
    ("parameter table names every template parameter", check_parameters),
    ("output table names every template output", check_outputs),
    ("in-page links resolve", check_anchors),
]
APP_CHECKS = [
    ("quoted messages come from the Exact strings table", check_exact_strings),
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--guide", type=Path, default=DEFAULT_GUIDE)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--app-repo", type=Path, help="checkout of ai-employee, branch aws-marketplace-offer")
    args = parser.parse_args(argv)
    try:
        ctx = {"guide": args.guide.read_text(encoding="utf-8"),
               "template": args.template.read_text(encoding="utf-8")}
        checks = list(CHECKS)
        if args.app_repo:
            ctx["app_doc"] = (args.app_repo / APP_STRINGS_FILE).read_text(encoding="utf-8")
            checks += APP_CHECKS
    except (OSError, UnicodeDecodeError) as e:
        print(f"ERROR: {e}")
        return 1
    failed = 0
    for label, check in checks:
        try:
            problems = check(ctx)
        except (ValueError, IndexError, re.error) as e:
            problems = [f"could not run: {type(e).__name__}: {e}"]
        if problems:
            failed += 1
            print(f"  {label}: FAILED")
            for problem in problems:
                print(f"    - {problem}")
        else:
            print(f"  {label}: ok")
    if failed:
        print(f"ERROR: {failed} of {len(checks)} guide checks failed in {args.guide}")
        return 1
    print(f"  {args.guide.name}: all {len(checks)} checks hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
