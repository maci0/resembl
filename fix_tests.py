import re

with open("tests/test_cli.py", "r") as f:
    text = f.read()

# Undo my previous sed change since `--format json` might be in weird places.
text = text.replace("--format json", "--json")


def move_global_flags(m):
    quote = m.group(1)
    cmd = m.group(2)

    # Check for --json and --no-color
    has_json = "--json" in cmd
    has_no_color = "--no-color" in cmd

    cmd = cmd.replace(" --json", "").replace("--json ", "").replace("--json", "")
    cmd = (
        cmd.replace(" --no-color", "")
        .replace("--no-color ", "")
        .replace("--no-color", "")
    )

    globals_prefix = ""
    if has_json:
        globals_prefix += "--format json "
    if has_no_color:
        globals_prefix += "--no-color "

    cmd = globals_prefix + cmd.strip()
    return f"{quote}{cmd}{quote}"


# We want to match contents inside quotes that look like CLI commands.
# Let's match run_command arguments.
text = re.sub(
    r'([f]?"|\')([\w\-\s\{\}\.\[\]\'\/\\]*?--(?:json|no-color)[\w\-\s\{\}\.\[\]\'\/\\]*?)\1',
    move_global_flags,
    text,
)

with open("tests/test_cli.py", "w") as f:
    f.write(text)
