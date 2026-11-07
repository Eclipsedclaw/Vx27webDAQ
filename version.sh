#!/usr/bin/env bash
# Version numbers of ALAB DAQ: vMAJOR.MINOR.PATCH, kept in src/alab_daq/__init__.py.
# Every change adds 1 to PATCH. MINOR (a bigger step) and MAJOR (major changes, releases) change only
# when you choose so:
#
#   ./version.sh                                      the version, its commit, and what changed since
#   ./version.sh patch|minor|major [-m NOTE]... [-- GIT_COMMIT_OPTIONS]
#   ./version.sh commit [-m NOTE]... [-- GIT_COMMIT_OPTIONS]
#                                                     the number as it is in __init__.py, after you changed
#                                                     it there by hand (no step)
#
# A step takes the notes written under "## Unreleased" in CHANGELOG.md, plus each -m NOTE as a line
# "- NOTE", and puts them under a new heading "## vX.Y.Z (date)". It writes the new number into
# __init__.py, commits every change in this folder as "vX.Y.Z: <first note>", and tags that commit vX.Y.Z.
# MINOR sets PATCH back to 0; MAJOR sets MINOR and PATCH back to 0. Nothing leaves this computer: it never
# pushes (git push origin main --tags does, when you choose). Options after -- go to git commit (e.g. --trailer "...").
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
init=src/alab_daq/__init__.py
log=CHANGELOG.md

fail() { echo "version.sh: $*" >&2; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "$PWD is not a git repository"
current="$(sed -n 's/^__version__ = "\([0-9]*\.[0-9]*\.[0-9]*\)"$/\1/p' "$init")"
[ -n "$current" ] || fail "no line __version__ = \"X.Y.Z\" in $init"

if [ $# -eq 0 ]; then
    echo "version  v$current"
    if git rev-parse -q --verify "refs/tags/v$current" >/dev/null; then
        echo "commit   $(git log -1 --format='%h %ad %s' --date=short "v$current")"
        n="$(git rev-list --count "v$current..HEAD")"
        [ "$n" = 0 ] || echo "         and $n commit(s) after it"
    else
        echo "commit   none has the tag v$current yet (./version.sh commit makes it one, as it is)"
    fi
    changes="$(git status --porcelain)"
    if [ -n "$changes" ]; then
        echo "changed  since the last commit (a step would commit them):"
        echo "$changes" | sed 's/^/         /'
    else
        echo "changed  nothing since the last commit"
    fi
    exit 0
fi

step="$1"
shift
notes=()
commit_options=()
while [ $# -gt 0 ]; do
    case "$1" in
        -m) notes+=("${2:?-m needs a note}"); shift 2 ;;
        --) shift; commit_options=("$@"); break ;;
        *) fail "unknown argument '$1' (see the top of version.sh)" ;;
    esac
done
IFS=. read -r major minor patch <<< "$current"
case "$step" in
    patch) patch=$((patch + 1)) ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    major) major=$((major + 1)); minor=0; patch=0 ;;
    commit) ;;                                  # the number was changed by hand: keep it
    *) fail "the step is patch, minor, major or commit, not '$step'" ;;
esac
new="$major.$minor.$patch"
if git rev-parse -q --verify "refs/tags/v$new" >/dev/null; then
    [ "$step" = commit ] && fail "v$new is committed already: change the number in $init first, or use patch, minor or major"
    fail "the tag v$new exists already"
fi

# CHANGELOG.md: the notes under "## Unreleased" move under "## vX.Y.Z (date)"; prints the first note
subject="$(python3 -I - "$log" "v$new" "$(date +%Y-%m-%d)" "${notes[@]}" <<'PY'
import re, sys
path, version, day, extra = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
text = open(path, encoding="utf-8").read()
m = re.search(r"^## Unreleased[ \t]*\n(.*?)(?=^## |\Z)", text, flags=re.M | re.S)
if m is None:
    sys.exit(f"version.sh: {path} has no heading '## Unreleased'")
body = "\n".join(line.rstrip() for line in m.group(1).splitlines()).strip("\n")
body = "\n".join(part for part in (body, "\n".join("- " + n for n in extra)) if part)
if not body.strip():
    sys.exit(f"version.sh: nothing to describe: write what changed under '## Unreleased' in {path}, or give -m NOTE")
text = text[:m.start()] + f"## Unreleased\n\n## {version} ({day})\n\n{body}\n\n" + text[m.end():]
open(path, "w", encoding="utf-8").write(text)
# commit subject: the first note (its lines joined), or only its label when it reads "Label: details"
first = re.split(r"\n\s*\n|\n(?=[-*] )", body.strip())[0]
first = " ".join(re.sub(r"^\s*[-*]\s*", "", line).strip() for line in first.splitlines())
label = first.split(": ", 1)[0]
first = label if ": " in first and len(label) <= 50 else first
print(first if len(first) <= 72 else first[:71].rstrip() + "…")
PY
)"
sed -i "s/^__version__ = \"$current\"\$/__version__ = \"$new\"/" "$init"
body="$(awk -v head="## v$new " 'index($0, head) == 1 {on = 1; next} on && /^## / {exit} on' "$log")"
git add -A
git commit -q -m "v$new: $subject" -m "$body" "${commit_options[@]}"
git tag -a "v$new" -m "v$new: $subject"
echo "v$current -> v$new: commit $(git rev-parse --short HEAD), tag v$new"
