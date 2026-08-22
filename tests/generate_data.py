"""Utility script to generate random assembly files for testing."""

import argparse
import os
import random
import textwrap

# --- Configuration ---
DEFAULT_NUM_FILES = 1000
DEFAULT_DATA_DIR = "data"

# --- Helper Data ---
REGISTERS_32 = ["eax", "ebx", "ecx", "edx", "esi", "edi", "ebp", "esp"]
REGISTERS_8 = ["al", "ah", "bl", "bh", "cl", "ch", "dl", "dh"]
ARITHMETIC_OPS = ["add", "sub", "and", "or", "xor", "inc", "dec", "neg"]
BITWISE_OPS = ["rol", "ror", "shl", "shr", "sal", "sar"]
STACK_OPS = ["push", "pop"]
DATA_OPS = ["mov", "lea", "xchg"]
CONTROL_FLOW_OPS = ["jmp", "call", "ret"]
CONDITIONAL_JUMPS = ["je", "jne", "jg", "jl", "jge", "jle", "jz", "jnz"]
TEST_OPS = ["test", "cmp"]

ALL_REGISTERS = REGISTERS_32 + REGISTERS_8


def get_random_operand(size: int = 32) -> str:
    """Returns a random operand (register, memory, or immediate)."""
    rand = random.random()
    if rand < 0.6:  # 60% chance of a register
        return random.choice(REGISTERS_32 if size == 32 else REGISTERS_8)
    if rand < 0.9:  # 30% chance of a memory operand
        base_reg = random.choice(REGISTERS_32)
        offset = random.randint(4, 256)
        prefix = "dword" if size == 32 else "byte"
        return f"{prefix} [{base_reg}+{offset:#x}]"
    # 10% chance of an immediate value
    return f"{random.randint(0, 0xFFFF):#x}"


# --- Code Block Generators ---


def generate_arithmetic_block(num_instructions: int) -> list[str]:
    """Generates a block of random arithmetic operations."""
    lines = ["; --- Arithmetic Block ---"]
    for _ in range(num_instructions):
        op = random.choice(ARITHMETIC_OPS)
        if op in ["inc", "dec", "neg"]:
            lines.append(f"    {op} {get_random_operand()}")
        else:
            lines.append(f"    {op} {get_random_operand()}, {get_random_operand()}")
    return lines


def generate_bitwise_block(num_instructions: int) -> list[str]:
    """Generates a block of random bitwise shift/rotate operations."""
    lines = ["; --- Bitwise Block ---"]
    for _ in range(num_instructions):
        op = random.choice(BITWISE_OPS)
        reg = get_random_operand()
        amount = random.choice([str(random.randint(1, 7)), "cl"])
        lines.append(f"    {op} {reg}, {amount}")
    return lines


def generate_stack_block(num_instructions: int) -> list[str]:
    """Generates a block of random stack operations."""
    lines = ["; --- Stack Manipulation ---"]
    for _ in range(num_instructions):
        op = random.choice(STACK_OPS)
        lines.append(f"    {op} {get_random_operand()}")
    return lines


def generate_data_block(num_instructions: int) -> list[str]:
    """Generates a block of random data movement operations."""
    lines = ["; --- Data Movement ---"]
    for _ in range(num_instructions):
        op = random.choice(DATA_OPS)
        lines.append(f"    {op} {get_random_operand()}, {get_random_operand()}")
    return lines


def generate_control_flow_block(label_id: str, function_names: list[str]) -> list[str]:
    """Generates a conditional jump block."""
    lines = ["; --- Control Flow Block ---"]
    op = random.choice(TEST_OPS)
    lines.append(f"    {op} {get_random_operand()}, {get_random_operand()}")
    jump_op = random.choice(CONDITIONAL_JUMPS)
    lines.append(f"    {jump_op} .label_{label_id}")

    # Add some other random instructions
    lines.extend(generate_arithmetic_block(random.randint(1, 2)))

    # Add a call or an unconditional jump
    if random.random() < 0.5 and function_names:
        lines.append(f"    call {random.choice(function_names)}")
    else:
        lines.append(f"    jmp .end_{label_id}")

    lines.append(f".label_{label_id}:")
    lines.extend(generate_data_block(1))
    lines.append(f".end_{label_id}:")
    return lines


# --- Main Generation Logic ---


def _generate_one(args: tuple[str, int, int, str, int | None]) -> None:
    """Worker: generate and write a single function file.

    Pure function (no shared state) so it can run in a process pool.  With a
    fixed seed, the global RNG is re-seeded per file (``seed + index``), so
    the output is deterministic regardless of pool scheduling.
    """
    name, i, num_files, data_dir, seed = args
    if seed is not None:
        random.seed(seed + i)
    function_names_sample = [f"generated_func_{random.randrange(num_files):04d}" for _ in range(20)]

    # --- Build the function body ---
    body = []
    num_blocks = random.randint(3, 8)
    block_generators = [
        generate_arithmetic_block,
        generate_stack_block,
        generate_data_block,
        generate_bitwise_block,
    ]

    for j in range(num_blocks):
        # Decide whether to generate a conditional block or a standard one
        if random.random() < 0.3:  # 30% chance of a control flow block
            body.extend(generate_control_flow_block(f"{i}_{j}", function_names_sample))
        else:
            generator = random.choice(block_generators)
            body.extend(generator(random.randint(1, 3)))
        body.append("")  # Add a blank line for spacing

    # --- Assemble the final file content ---
    content = (
        [
            f"; Function: {name}",
            "; Description: A highly randomized, auto-generated function.",
            "section .text",
            f"global {name}",
            f"{name}:",
            "    push    ebp",
            "    mov     ebp, esp",
            f"    sub     esp, {random.randint(4, 64)} ; Allocate stack space",
        ]
        + body
        + [
            "    mov     esp, ebp",
            "    pop     ebp",
            "    ret",
        ]
    )

    # Write the file
    file_path = os.path.join(data_dir, f"{name}.asm")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("\n".join(textwrap.indent(line, "    ") for line in content))


def generate_files(
    data_dir: str = DEFAULT_DATA_DIR,
    num_files: int = DEFAULT_NUM_FILES,
    jobs: int | None = None,
    seed: int | None = None,
):
    """Generate random assembly files in data_dir.

    Generation is embarrassingly parallel (each file is independent), so a
    process pool is used by default.  Pass ``jobs=1`` to force a single
    process (e.g. inside a fork-sensitive environment).  A fixed *seed*
    makes the output deterministic (reproducible benchmarks): each file
    derives its randomness from ``seed + index``, so the pool order never
    matters.
    """
    import multiprocessing as _mp
    from concurrent.futures import ProcessPoolExecutor

    if not os.path.exists(data_dir):
        os.makedirs(data_dir)

    # Clean out old generated files
    for filename in os.listdir(data_dir):
        if filename.startswith("generated_func_"):
            os.remove(os.path.join(data_dir, filename))

    function_names = [f"generated_func_{i:04d}" for i in range(num_files)]

    # Generation is now O(n); the process pool only pays off for large runs
    # (spawning workers costs a couple of seconds regardless of size).
    if jobs is None:
        jobs = max(1, os.cpu_count() or 1) if num_files > 50_000 else 1
    jobs = min(jobs, max(1, num_files))

    args = [(name, i, num_files, data_dir, seed) for i, name in enumerate(function_names)]

    if jobs == 1:
        for a in args:
            _generate_one(a)
    else:
        ctx = _mp.get_context("spawn")
        try:
            with ProcessPoolExecutor(max_workers=jobs, mp_context=ctx) as executor:
                list(executor.map(_generate_one, args))
        except Exception:
            # Fall back to in-process generation if the pool is unavailable.
            for a in args:
                _generate_one(a)

    print(f"Successfully generated {num_files} new, more random assembly files in '{data_dir}/'")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate random assembly files for testing.")
    parser.add_argument(
        "--num-files",
        type=int,
        default=DEFAULT_NUM_FILES,
        help=f"Number of files to generate (default: {DEFAULT_NUM_FILES}).",
    )
    parser.add_argument(
        "--data-dir",
        type=str,
        default=DEFAULT_DATA_DIR,
        help=f"Directory to store generated files (default: '{DEFAULT_DATA_DIR}').",
    )
    args = parser.parse_args()
    generate_files(data_dir=args.data_dir, num_files=args.num_files)
