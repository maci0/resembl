import re

with open("resembl/cli.py", "r") as f:
    content = f.read()

# Add csv to imports
content = re.sub(r"import json\n", "import csv\nimport json\n", content)

# Add format to State
content = re.sub(
    r"quiet: bool = False\n    no_color: bool = False",
    'quiet: bool = False\n    no_color: bool = False\n    format: str = "table"',
    content,
)

# Add format_opt to app_callback
content = re.sub(
    r'no_color: bool = typer\.Option\(False, "--no-color", help="Disable colored output."\),\n\)',
    'no_color: bool = typer.Option(False, "--no-color", help="Disable colored output."),\n    format_opt: str | None = typer.Option(None, "--format", help="Output format: table, json, csv. Overrides config."),\n)',
    content,
)

# Update app_callback body
content = re.sub(
    r"state\.no_color = no_color",
    'state.no_color = no_color\n    state.format = format_opt or state.config.get("format", "table")',
    content,
)

# Replace _echo_json with _echo_format (definition)
echo_json_def = """def _echo_json(data: object) -> None:
    \"\"\"Print data as JSON unless ``--quiet`` is set.\"\"\"
    if not state.quiet:
        console.print_json(json.dumps(data, indent=2))"""

echo_format_def = """def _echo_format(data: object) -> None:
    \"\"\"Print data in the requested format (JSON/CSV) unless ``--quiet``.\"\"\"
    if state.quiet:
        return
    if state.format == "csv":
        import sys
        if isinstance(data, dict) and "matches" in data:
            data = data["matches"]
        if isinstance(data, list) and data and isinstance(data[0], dict):
            for row in data:
                if "names" in row and isinstance(row["names"], list):
                    row["names"] = ", ".join(row["names"])
            writer = csv.DictWriter(sys.stdout, fieldnames=data[0].keys())
            writer.writeheader()
            writer.writerows(data)
        elif isinstance(data, dict):
             for k, v in data.items():
                 if isinstance(v, list):
                     data[k] = ", ".join(v)
             writer = csv.DictWriter(sys.stdout, fieldnames=data.keys())
             writer.writeheader()
             writer.writerow(data)
        else:
             console.print_json(json.dumps(data, indent=2))
    else:
        console.print_json(json.dumps(data, indent=2))"""

content = content.replace(echo_json_def, echo_format_def)

# Remove json_output from command definitions
content = re.sub(
    r'\s*json_output: bool = typer\.Option\(False, "--json", help="Output.*?"\),',
    "",
    content,
)
content = re.sub(
    r'json_output: bool = typer\.Option\(False, "--json", help="Output.*?"\)',
    "",
    content,
)

# Replace `if json_output:` with `if state.format in ("json", "csv"):`
content = re.sub(r"if json_output:", 'if state.format in ("json", "csv"):', content)
content = re.sub(
    r"disable=state\.quiet or json_output",
    'disable=state.quiet or state.format in ("json", "csv")',
    content,
)

# Replace `_echo_json` calls with `_echo_format`
content = content.replace("_echo_json", "_echo_format")

with open("resembl/cli.py", "w") as f:
    f.write(content)
print("Done")
