#!/usr/bin/env python3
# pylint: disable=import-error
"""A fuzzer for the code_tokenize function."""

import sys

import atheris

with atheris.instrument_imports():
    from resembl.scoring import code_tokenize


def test_one_input(data):
    """The entry point for the fuzzer."""
    try:
        fdp = atheris.FuzzedDataProvider(data)
        string_data = fdp.ConsumeUnicode(fdp.remaining_bytes())
        code_tokenize(string_data)
    except UnicodeDecodeError:
        # Expected on non-UTF-8 input; not a finding.
        pass


def main():
    """Main function to run the fuzzer."""
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
