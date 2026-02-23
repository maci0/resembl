# Contributing to resembl

## Part 1: A Foundation for Collaboration
This document outlines the development standards, workflows, and best practices for contributing to the resembl project. Adhering to these guidelines ensures consistency, quality, and a positive and productive environment for everyone.

### Welcome to the resembl Project!
Thank you for your interest in contributing to resembl! We are thrilled to have you here. This project thrives on community involvement, and we value contributions of all kinds, from filing detailed bug reports and proposing new features to improving documentation and submitting code changes. Every contribution helps make resembl better.

This guide is designed to make the contribution process as clear and straightforward as possible. Whether you are a first-time open-source contributor or a seasoned developer, we hope you find this document helpful. The goal is to create a welcoming space where we can collaborate effectively to build a great tool.  

### Our Core Philosophy
The central goal of resembl is to build a command-line tool that is robust, user-friendly, and maintainable for the long term. To achieve this, our development is guided by several core principles:

- **User-Centricity:** We build for our users. Every feature, fix, and decision should be guided by the need to create a tool that is reliable, intuitive, and solves real-world problems effectively.
- **Developer Experience:** We value our contributors. The development process itself should be smooth, well-documented, and rewarding. We strive to provide clear guidelines and automated tooling to make contributing a positive experience.  
- **Quality and Maintainability:** We are committed to writing clean, well-tested, and thoroughly documented code. This ensures the project's long-term health and makes it easier for new contributors to get involved.  
- **Iterative Improvement:** We favor small, well-defined, and incremental changes over large, monolithic pull requests. This approach makes reviews more manageable, reduces the risk of introducing bugs, and allows the project to evolve steadily.

## Part 2: Getting Started: Your Development Environment
This section provides a streamlined, one-time setup process to get the resembl codebase running on your local machine.

### Prerequisites
Before you begin, please ensure you have the following software installed on your system:

- Git
- Python 3.11 or newer
- uv for dependency and environment management.  

### One-Time Setup
Follow these steps to create a local development environment. This workflow uses modern tooling to ensure a consistent and reproducible setup for all contributors.  

1.  **Fork the Repository**
    Navigate to the resembl GitHub repository and click the "Fork" button in the top-right corner. This creates a personal copy of the project under your GitHub account.

2.  **Clone Your Fork**
    Clone your forked repository to your local machine. Replace `YOUR-USERNAME` with your actual GitHub username.
    ```bash
    git clone git@github.com:YOUR-USERNAME/resembl.git
    ```

3.  **Navigate to the Project Directory**
    ```bash
    cd resembl
    ```
4.  **Create and Activate the Virtual Environment**
    This command creates a virtual environment in the `.venv` directory.
    ```bash
    uv venv
    ```
    Activate the virtual environment. You will need to do this every time you open a new terminal session to work on the project.
    ```bash
    source .venv/bin/activate
    ```

5.  **Install Dependencies**
    This command reads the `pyproject.toml` file and installs all necessary development and runtime dependencies into the virtual environment.
    ```bash
    uv pip install -e .[dev]
    ```
6.  **Install Pre-Commit Hooks**
    This is a critical step for automating quality checks. This command sets up Git hooks that will automatically run formatters and linters on your code before each commit.
    ```bash
    uv run pre-commit install
    ```
This setup process is designed to "shift quality left," moving the responsibility for basic code health checks from the final review stage to the developer's local machine. The old way involved a contributor manually running a checklist of commands (pytest, mypy, black, etc.), which was error-prone and led to frustrating cycles of CI failures and fixes. The new, automated workflow using pre-commit hooks  ensures that every commit is already vetted for style, formatting, and common errors. This frees the contributor from remembering the checklist and allows the human reviewer to focus on the more important aspects of the change, such as its logic and architecture. This automation transforms quality assurance from a manual chore into an invisible, supportive guardrail, making the contribution process faster and more pleasant for everyone.  

## Part 3: The Development Lifecycle
Once your environment is set up, you are ready to start contributing. This section outlines the typical workflow for making a change to resembl.

### Find or Create an Issue
All work should be tracked via the GitHub Issues tab. This practice encourages communication and prevents multiple people from working on the same thing or effort being wasted on a change that doesn't align with the project's direction.  

- **For New Contributors:** A great place to start is by looking for issues tagged with `good first issue`. These are typically well-defined, smaller tasks that are perfect for getting familiar with the codebase and contribution process.  
- **For All Contributors:** Before starting work on a new feature, a significant refactor, or a complex bug fix, please check if an issue already exists. If not, open a new one to discuss the proposed change with the maintainers. This ensures everyone is aligned on the approach before any code is written.

### Create a Branch
Never work directly on the `main` branch. For every contribution, create a new feature branch from an up-to-date `main` branch. We use a descriptive naming convention to keep the repository organized:

```bash
# General format: git checkout -b <type>/<issue-number>-short-description
# Example for a new feature:
git checkout -b feat/123-add-json-output

# Example for a bug fix:
git checkout -b fix/145-handle-api-errors
```
This convention, which incorporates the Conventional Commit type and the issue number, provides valuable context at a glance.  

### Write Code, Write Tests
The resembl project follows a test-driven approach to ensure quality and correctness.

- **Test-Driven Development (TDD):** We strongly encourage TDD.
    - For new features, please write a failing test that captures the feature's requirements before you implement the feature itself.
    - For bug fixes, first write a test that reproduces the bug. This verifies the bug's existence and provides a clear signal when the fix is working correctly.

- **Run Tests Locally:** You can run the full test suite at any time with the following command:
    ```bash
    uv run pytest
    ```
    While the pre-commit hook may run tests on changed files, it is good practice to run the entire suite before submitting your work to catch any unintended side effects.

- **Check Test Coverage:** To ensure that your changes are well-tested, you can generate a test coverage report. This project uses `pytest-cov` to measure how much of the codebase is exercised by the tests.
    ```bash
    uv run pytest --cov=resembl
    ```
    This command will run the test suite and then print a report to the console, showing the percentage of code covered by tests for each file. Aim to maintain or increase the coverage percentage with your contributions.

- **Running Fuzzers:** This project uses fuzz testing to find bugs and crashes in core, security-sensitive functions. The fuzzers are located in the `fuzzers/` directory and are built on the `atheris` engine. You can run them locally to test for issues.

    To run a specific fuzzer, execute its script directly. For example, to run the fuzzer for the `get_tokens` function:
    ```bash
    uv run ./fuzzers/fuzz_get_tokens.py
    ```
    The fuzzer will run indefinitely until you stop it manually (with `Ctrl+C`) or until it finds a crash. To run it for a fixed duration, use the `-max_total_time` flag:
    ```bash
    uv run ./fuzzers/fuzz_get_tokens.py -max_total_time=60
    ```
    If a crash is found, the fuzzer will stop and create a `crash-<hash>` file in the root directory containing the input that caused the failure. This file is crucial for debugging and should be included in any bug report.

- **Code Style and Quality:** As configured during setup, our pre-commit hooks will automatically run `black` for formatting, `ruff` for linting, and `mypy` for static type checking before each commit. You do not need to run these tools manually. If a hook fails, the commit will be aborted, and you will see an error message indicating what needs to be fixed. Simply correct the issue and attempt the commit again.  

### Commit Your Changes
Once your code and tests are ready, stage your changes and commit them. This action will trigger the pre-commit hooks.

```bash
git add .
git commit
```
The commit message itself is a crucial part of your contribution and must follow a specific format, as detailed in the next section.

## Part 4: Quality, Standards, and Conventions
This section details the core standards that ensure the long-term health, consistency, and maintainability of the resembl project.

### Commit Message Guidelines: The Conventional Commits Standard
To maintain a clear, navigable, and machine-readable Git history, resembl strictly adheres to the Conventional Commits specification v1.0.0. This is not just for aesthetic reasons; a structured commit history allows us to automate changelog generation and semantic versioning, which is critical for project maintenance.  

A commit message must be structured as follows:
```
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```
- **Type:** A noun that describes the category of change. This is mandatory. See the table below for allowed types.
- **Scope (Optional):** A noun in parentheses that provides a contextual clue about what part of the codebase is affected (e.g., `(parser)`, `(api)`, `(auth)`).
- **Description:** A concise, imperative-mood summary of the change, starting with a lowercase letter and without a period at the end.
- **Body (Optional):** A more detailed explanation of the change, focusing on the "what" and "why." It should be separated from the description by a blank line.
- **Footer (Optional):** Used for referencing issue numbers (e.g., `Fixes #123`) or indicating breaking changes. It should be separated from the body by a blank line.

**Breaking Changes:** A change that breaks backward compatibility MUST be indicated. This can be done in two ways:
1.  Append a `!` after the type/scope (e.g., `feat(api)!:`).
2.  Include a `BREAKING CHANGE:` section in the footer.  

**Examples:**

A simple fix:
```
fix: resolve issue with form submission not triggering validation
```
A new feature with a scope:
```
feat(lang): add polish language support
```
A commit with a body and footer:
```
docs: add auth service instructions to README

The previous documentation was missing key steps for configuring
the authentication service, leading to confusion for new users.
This update provides a step-by-step guide.

Closes #78
```
A breaking change:
```
refactor(auth)!: overhaul token generation mechanism

The token generation logic is updated to use JWTs instead of
the previous opaque strings. This improves security and statelessness.

BREAKING CHANGE: The format of authentication tokens has changed.
All clients will need to be updated to handle JWTs.
```
### Function Naming Conventions

To improve readability and consistency, function names should follow a `noun_verb` or `noun_noun_verb` pattern. This convention makes it clear what object the function operates on and what action it performs.

- **`db_verb`**: For functions that operate on the database as a whole (e.g., `db_clean`, `db_reindex`).
- **`snippet_verb`**: For functions that operate on individual snippets (e.g., `snippet_add`, `snippet_delete`).
- **`snippet_name_verb`**: For functions that manage the names of a snippet (e.g., `snippet_name_add`).

This consistent structure helps developers quickly understand the purpose of a function just by its name.

### Conventional Commit Types
Use the following table as a reference for choosing the correct commit type. Sticking to these types is essential for our automation tools.  

| Type     | Title                | Description                                                  | When to Use                                            |
| :------- | :------------------- | :----------------------------------------------------------- | :----------------------------------------------------- |
| `feat`   | Features             | A new feature for the user.                                  | When you add a new functionality.                      |
| `fix`    | Bug Fixes            | A bug fix for the user.                                      | When you fix a bug.                                    |
| `docs`   | Documentation        | Changes to the documentation only.                           | When you add or update README, docstrings, etc.        |
| `style`  | Styles               | Formatting, missing semi-colons, etc.                        | Code style changes that do not affect the logic.       |
| `refactor`| Code Refactoring     | A code change that neither fixes a bug nor adds a feature.   | When you improve the code structure without changing behavior. |
| `test`   | Tests                | Adding missing tests or correcting existing tests.           | When you add or modify tests.                          |
| `perf`   | Performance          | A code change that improves performance.                     | When you make the code faster or more efficient.       |
| `ci`     | Continuous Integration| Changes to CI configuration files and scripts.               | When you modify GitHub Actions, etc.                   |
| `build`  | Build System         | Changes that affect the build system or external dependencies. | When you modify `pyproject.toml`, Dockerfiles, etc.    |
| `chore`  | Chores               | Other changes that don't modify `src` or `test` files.       | For maintenance tasks like releasing a new version.    |

### Documentation is Not Optional
In this project, untested code is considered broken, and an undocumented feature is considered incomplete. Any change that affects users, developers, or the system's behavior must be accompanied by corresponding documentation updates.  

This includes:

- **Docstrings:** All new modules, classes, and functions must have clear, concise docstrings explaining their purpose, arguments, and return values.
- **README.md:** Update this file if your change affects installation, core concepts, or basic usage.
- **Project Documentation:** Update the relevant files in the `docs/` directory, such as `user_stories.md` or `flowcharts.md`, to reflect any changes to features or workflows.
- **Changelog:** While the `CHANGELOG.md` file is updated automatically during a release, your pull request description should be clear and comprehensive, as it will be used to generate the changelog entry.

### Managing Dependencies
Dependencies represent a long-term maintenance cost and security liability. Therefore, they should be added sparingly and only when they provide significant value that cannot be reasonably achieved otherwise.

- **Principle of Parsimony:** Before adding a new dependency, consider if the functionality can be implemented with the existing toolset.
- **Discussion First:** If you believe a new dependency is necessary, please open an issue first to discuss its purpose, benefits, and potential alternatives with the maintainers.
- **Adding a Dependency:** If a new dependency is approved, add it to the project using uv. Do not manually edit `pyproject.toml`.
    ```bash
    # For a runtime dependency
    uv pip install <package-name>

    # For a development-only dependency
    uv pip install -e .[dev]
    ```
Using `uv` ensures that both `pyproject.toml` is updated correctly, guaranteeing reproducible builds for all contributors.  

## Part 5: Submitting Your Contribution
This final part walks you through the process of getting your work reviewed and merged into the project.

### Preparing Your Pull Request
Before opening a pull request, please run through this pre-flight checklist to ensure your submission is in good shape.

1.  **Run the Full Test Suite:** Ensure all tests pass locally.
    ```bash
    uv run pytest
    ```
2.  **Update Your Branch:** Make sure your branch is up-to-date with the latest changes from the `resembl` main branch. This helps avoid merge conflicts.
    ```bash
    # Fetch the latest changes from the upstream repository
    git fetch upstream

    # Rebase your branch on top of the latest main
    git rebase upstream/main
    ```
    (Note: If you have not configured an `upstream` remote, you can do so with `git remote add upstream https://github.com/maci0/resembl.git`)

3.  **Push Your Branch:** Push your changes to your fork on GitHub.
    ```bash
    git push --force-with-lease origin feat/123-add-json-output
    ```
### Opening the Pull Request
You are now ready to open a pull request on GitHub.

- **Clear Title:** The pull request title should be clear and concise. It's good practice to use the format of your primary commit message.
- **Detailed Description:** Fill out the pull request template provided. This is your opportunity to explain your change to the maintainers.
- **Link the Issue:** Make sure to include a line like `Closes #123` in the description. This will automatically link the PR to the issue and close the issue when the PR is merged.
- **Summarize the "What" and "Why":** Briefly describe what the change does and why it's necessary.
- **Self-Review:** Before submitting, click on the "Files Changed" tab and review your own changes one last time. This is a great way to catch typos or small mistakes.

### The Review Process
Once your pull request is submitted, it will go through a review process. Here is what to expect:

- **Automated Checks:** As soon as you open the pull request, GitHub Actions will automatically run our full CI pipeline. This includes running the test suite on multiple Python versions and other quality checks. The PR cannot be merged if these checks fail.
- **Human Review:** A project maintainer will review your code for correctness, architectural soundness, and adherence to project standards.
- **Responding to Feedback:** It is common for reviewers to request changes. This is a normal and healthy part of the collaborative process. Please engage in the discussion and address the feedback by pushing new commits to your branch. The pull request will update automatically.
- **Approval and Merge:** Once all automated checks are passing and a maintainer has approved the changes, your pull request will be merged.

Congratulations, and thank you! Your contribution is now part of the `resembl` project. We deeply appreciate your time and effort.
