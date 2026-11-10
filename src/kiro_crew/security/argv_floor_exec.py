"""Which words of a frame bash runs as a program: the ssh floor's execute-position read.

A glob that can name an ssh verb (``s?h``) is an ssh invocation only where the shell
EXECUTES it.  This owner answers "is this word executed?" for one token frame: program
position and wrapper-resolved programs count; operands, array elements and words inside
a command substitution do not.  A heredoc body that a known reader certainly takes as
data is cut from the text the execute-position read sees
(:func:`_text_without_data_heredocs`, over the one heredoc reader
``shell_commands.read_commands``).  The ssh floor (``argv_floor_ssh``) reads the
answer; the facade is ``argv_floor``.
"""

from __future__ import annotations

import re

from . import argv_rules as _argv_rules
from . import shell_normalizer as _shell_normalizer


def _executed_program_indices(tokens: "list[str]") -> "set[int]":
    """Indices in *tokens* that bash runs as a PROGRAM, wrappers resolved.

    A glob that can name an ssh verb (``s?h``, ``s*``) is only an ssh
    invocation when it sits where the shell executes it: program position, or
    the program a command-running wrapper runs (``env``/``sudo``/``time``/
    ``nice``/``timeout``/``nohup``/``command``/``xargs`` …).  Anywhere else
    (``tar … s* *.*``, ``find s* l*``, ``sha1sum s* *.*``) it is a filename.
    Command boundaries (comments, separators) come from the one splitter,
    ``argv_rules._split_commands_indexed``; the program within each command and
    its wrapper set come from ``argv_rules._program_index`` -- neither is
    re-derived here, so a comment, ``case`` or wrapper shape handled there needs
    no second fix in this floor.  A word inside an array literal (``files=( s*
    *.* )``) is an element, not a program, so the repo's own array reader
    (``shell_normalizer._array_element_indices``) takes those positions out.
    ``find -exec`` subcommands and a spaced/alternated ``case`` arm are the
    documented residual: a glob verb in either is not marked executed and no
    existing reader tells the arm pattern from a command word.  A word inside a
    command substitution (``n=$(ls s* *.*)``, ``n=`ls s* *.*```) belongs to the
    substitution, which the payload walk reads as its own frame; in this frame
    the resolver would step over the assignment and take the substitution's
    first operand for the program, so those positions are taken out too.
    """
    executed: set[int] = set()
    array_elements = _shell_normalizer._array_element_indices(tokens)
    inside_substitution = _substitution_positions(tokens)
    # A control operator glued inside a word (``x;s?h``, ``x&&sudo``) ends one
    # command and starts the next in that word.  The splitter cuts only at a
    # word's end, so such a word is handed over as its parts, each keeping its index.
    # A quoted operator arrives as a sentinel, so one seen here is real.  A word
    # of a comment is not cut: the splitter ends a comment only at a lone ``;``.
    words: list[str] = []
    origins: list[int] = []
    in_comment = False
    for i, token in enumerate(tokens):
        in_comment = token != ";" if in_comment else token.startswith("#")
        start, end = (0, 0) if in_comment else _glued_operator(token)
        for word in (token[:start], token[start:end], token[end:]) if end else (token,):
            words.append(word)
            origins.append(i)
    for command in _argv_rules._split_commands_indexed(words):
        idx = _argv_rules._program_index([word for _, word in command])
        if idx is None:
            continue
        origin = origins[command[idx][0]]
        # An array element is a filename bash never runs; the opening ``name=(``
        # is an assignment the resolver already steps over.
        if origin in array_elements or origin in inside_substitution:
            continue
        # A word ending in ``)`` in command position is a ``case`` arm pattern
        # (``s*)``) or a subshell close, not a program bash runs.
        if not command[idx][1].endswith(")"):
            executed.add(origin)
    return executed


def _glued_operator(token: str) -> "tuple[int, int]":
    """The span of the last control operator with a word after it in *token*, or (0, 0).

    ``x&&s?h`` gives (1, 3).  A redirect word (``2>&1``, ``>|f``) is cut too, which
    changes nothing: the ssh walk reads no verb in a word that holds a redirect.
    """
    spans = [m.span() for m in _shell_normalizer._CONTROL_OPERATOR_RE.finditer(token)]
    return next((span for span in reversed(spans) if span[1] < len(token)), (0, 0))


def _substitution_positions(tokens: "list[str]") -> "set[int]":
    """Indices of the words of the FIRST command inside a ``$( … )`` or backtick span.

    ``$(`` and ``)`` are counted on their own.  Backticks are read only by parity:
    a word holding a complete pair (```date``;``) opens and closes inside itself,
    so it changes nothing, while a lone backtick opens or closes a span across
    words.  (``shell_normalizer._substitution_depth_delta`` counts a backtick PAIR
    as one open level that no ``)`` ever closes, so it cannot be used here.)  The
    word that opens a substitution is not inside it.

    Only the span's first command is taken out: it is the one the resolver misreads
    (it steps over ``n=$(ls`` as an assignment).  After a separator the resolver
    finds each program itself, so those words stay in.  The tokens have lost their
    quotes, so a quoted backtick opens a span here that bash never opens; ending
    the span's effect at the first separator keeps a command between two quoted
    backticks in execute position.
    """
    inside: set[int] = set()
    depth = 0
    in_backticks = False
    first_command = True
    for i, token in enumerate(tokens):
        ends_command = _shell_normalizer._ends_argv(token)
        if (depth > 0 or in_backticks) and first_command and not ends_command:
            inside.add(i)
        depth = max(0, depth + token.count("$(") - token.count(")"))
        if token.count("`") % 2:
            in_backticks = not in_backticks
        if depth == 0 and not in_backticks:
            first_command = True
        elif ends_command:
            first_command = False
    return inside


#: Readers whose stdin is data whatever their arguments are.
_PLAIN_HEREDOC_READERS = frozenset({"cat", "tee", "jq"})
#: ``git`` built-ins that read stdin as data, each with the words that make it do
#: so (empty: it reads stdin unasked).  A built-in cannot be aliased; any other
#: subcommand can be an alias that runs a shell.  The floor reads lowercased text.
_GIT_STDIN_READERS: "dict[str, tuple[tuple[str, ...], ...]]" = {
    "commit": (("-f", "-"), ("--file", "-"), ("--file=-",), ("-f-",)),
    "apply": (),
    "am": (),
    "mktree": (),
    "hash-object": (("--stdin",),),
    "update-index": (("--stdin",), ("--index-info",)),
}
#: ``gh`` core commands that take a body from stdin, with the words that ask for it.
_GH_STDIN_READERS: "dict[str, tuple[tuple[str, ...], ...]]" = {
    "pr": (("--body-file", "-"), ("--body-file=-",), ("-f", "-")),
    "issue": (("--body-file", "-"), ("--body-file=-",), ("-f", "-")),
    "release": (("--notes-file", "-"), ("--notes-file=-",), ("-f", "-")),
    "api": (("--input", "-"), ("--input=-",)),
}
#: Words that open or close a compound command, so a statement after one may be nested.
_COMPOUND_WORDS = frozenset(
    "{ } if then elif else fi for while until do done case esac select function time ! [[ coproc".split()
)
_READER_PROGRAM_RE = re.compile(r"(cat|tee|jq|git|gh)(?:\.exe)?")
_REDIRECT_RE = re.compile(r"\d*(<<-?|>>?)(.*)", re.DOTALL)
_PLAIN_PATH_RE = re.compile(r"[\w./~+-]+")


def _has_words(words: "list[str]", wanted: "tuple[str, ...]") -> bool:
    """True when *wanted* appears in *words* as consecutive words."""
    span = len(wanted)
    return any(tuple(words[i : i + span]) == wanted for i in range(len(words) - span + 1))


def _reads_stdin_as_data(command: object) -> bool:
    """True when *command* is a known reader in a known form: its stdin is only data.

    The program is a bare ``cat``/``tee``/``jq``/``git``/``gh`` word (no path, no
    assignment prefix, no wrapper).  ``git`` and ``gh`` count only when the first
    argument is a built-in from the tables above and the words that make it read
    stdin are present, so a global option (``git -c alias.x=!sh``) or an alias or
    extension (``git x``, ``gh some-ext``) is not a reader.
    """
    words = [word.raw.replace("\\\n", "") for word in command.argv]  # type: ignore[attr-defined]
    words = [word for word in words if word]
    match = _READER_PROGRAM_RE.fullmatch(words[0]) if words else None
    if match is None:
        return False
    program = match.group(1)
    if program in _PLAIN_HEREDOC_READERS:
        return True
    forms = (_GIT_STDIN_READERS if program == "git" else _GH_STDIN_READERS).get(
        words[1] if len(words) > 1 else ""
    )
    if forms is None:
        return False
    return not forms or any(_has_words(words[2:], form) for form in forms)


def _statement_is_plain(command: object) -> bool:
    """True when *command* expands nothing and redirects only to plain file paths."""
    from .shell_commands import SUBST  # local: the facade serves every module name

    for step in command.steps:  # type: ignore[attr-defined]
        if step.text == SUBST or (step.text in ("$", "`") and step.state != 1):
            return False
    for word, target in command.redirects:  # type: ignore[attr-defined]
        match = _REDIRECT_RE.fullmatch(word.raw)
        if match is None or "<<<" in word.raw:
            return False
        if match.group(1).startswith(">"):
            path = match.group(2) or (target.raw if target is not None else "")
            if not _is_plain_path(path):
                return False
    return True


def _is_plain_path(path: str) -> bool:
    """True when *path* is a bare path word, or one quoted word that expands nothing."""
    if len(path) > 2 and path[0] == path[-1] and path[0] in "\"'":
        inner = path[1:-1]
        return not any(char in inner for char in (path[0], "$", "`", "*", "?", "[", "\n"))
    return _PLAIN_PATH_RE.fullmatch(path) is not None


def _text_without_data_heredocs(text_lower: str) -> str:
    """*text_lower* without the heredoc bodies that are certainly only data.

    ``shell_commands.read_commands`` is the one reader that pairs a ``<<`` with its
    closing tag; this floor pairs nothing itself.  A body is cut only when ALL of
    these hold, and stays in the text otherwise:

    * nothing in the text nests a statement (no parenthesis, backtick, substitution
      or compound-command word), so each pipeline (``shell_commands.pipelines``) is
      a top-level statement;
    * every segment is a known reader in a known form (:func:`_reads_stdin_as_data`),
      expands nothing and redirects only to plain file paths
      (:func:`_statement_is_plain`);
    * every heredoc of the pipeline has a quoted tag and a closing tag line.
    """
    if "<<" not in text_lower:
        return text_lower
    from .shell_commands import SUBST, pipelines, read_commands  # local, as above

    commands = read_commands(text_lower, heredocs=True)
    for command in commands:
        words = command.words
        if (
            command.separator in ("(", ")", "`")
            or any(step.text == SUBST for step in command.steps)
            or (words and words[0].raw in _COMPOUND_WORDS)
        ):
            return text_lower
    cuts: "list[tuple[int, int]]" = []
    for pipeline in pipelines(commands):
        docs = [doc for command in pipeline for doc in command.heredocs]
        if (
            docs
            and all(_reads_stdin_as_data(c) and _statement_is_plain(c) for c in pipeline)
            and all(doc.quoted and doc.end != doc.tail for doc in docs)
        ):
            cuts += [(doc.start, doc.end) for doc in docs]
    kept, prev = [], 0
    for start, end in sorted(cuts):
        kept.append(text_lower[prev:start])
        prev = end
    return "".join(kept) + text_lower[prev:]
