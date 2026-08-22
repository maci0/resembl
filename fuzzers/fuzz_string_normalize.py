#!/usr/bin/env python3
# pylint: disable=duplicate-code,import-error
"""A fuzzer for the string_normalize function."""

import sys

import atheris

with atheris.instrument_imports():
    from resembl.scoring import string_normalize


def test_one_input(data):
    """The entry point for the fuzzer."""
    try:
        fdp = atheris.FuzzedDataProvider(data)
        string_data = fdp.ConsumeUnicode(fdp.remaining_bytes())
        string_normalize(string_data)
    except UnicodeDecodeError:
        # Expected on non-UTF-8 input; not a finding.
        pass


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
