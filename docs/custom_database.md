# Using a Custom Database with resembl

The `resembl` library is designed to be flexible, allowing you to integrate it with your own application's database infrastructure. Instead of being locked into the default `sqlite:///assembly.db` file, you can provide your own database engine. This is possible because all core `resembl` functions operate on a `Session` object that you provide, a design principle known as **Dependency Injection**.

This guide will walk you through the process of using `resembl` with a custom database managed by your application.

## The Key Principle: SQLModel Metadata

The magic behind this flexibility lies in how `SQLModel` manages database schemas. When you import a model class that inherits from `SQLModel` (like `resembl.models.Snippet`), it registers its schema with a central `SQLModel.metadata` object.

When you are ready to create your database tables, you call `SQLModel.metadata.create_all(your_engine)`. This command iterates through all registered models—both yours and `resembl`'s—and creates the corresponding tables in the database pointed to by your engine.

## Step-by-Step Example

Here is a complete example of how an external application can set up its own database and use `resembl` to manage assembly snippets within it.

```python
# main_app.py
from sqlmodel import SQLModel, create_engine, Session

# 1. Import the resembl.models you need.
#    This is the crucial step that registers the `Snippet` model's schema
#    with SQLModel's central metadata catalog.
from resembl.models import Snippet

# 2. Import the resembl core functions you want to use.
#    These functions are designed to work with any compatible session.
from resembl.core import snippet_add, snippet_list

# 3. Create your application's custom database engine.
#    For this example, we'll use a temporary in-memory SQLite database,
#    but you could replace this with a PostgreSQL, MySQL, or any other
#    SQLAlchemy-compatible database URL.
my_custom_engine = create_engine("sqlite:///:memory:")

# 4. Create the tables on your custom engine.
#    Because we imported `Snippet`, this call will generate and execute the
#    `CREATE TABLE` statement for the 'snippet' table in our database.
SQLModel.metadata.create_all(my_custom_engine)

# 5. Use your engine to create a session and pass it to resembl functions.
#    From this point on, you interact with resembl by passing your session.
with Session(my_custom_engine) as session:
    print("Adding a snippet using our custom engine...")

    # Call a core resembl function with our session
    new_snippet = snippet_add(
        session=session,
        name="my_first_func",
        code="mov eax, 1; ret"
    )

    print(f"Snippet added! Checksum: {new_snippet.checksum}")

    # Verify the snippet was added to our custom database
    all_snippets = snippet_list(session)
    print(f"Found {len(all_snippets)} snippet(s) in our database.")
    print(f"Retrieved from custom DB: {all_snippets[0].name_list}")

```

## Summary of the Workflow

To use your own database with `resembl`, follow these steps:

1.  **Import Models:** Before creating your tables, make sure to `import` the `resembl` models you intend to use (e.g., `from resembl.models import Snippet`).
2.  **Create Your Engine:** Instantiate your own `create_engine()` with the desired database URL.
3.  **Create Tables:** Call `SQLModel.metadata.create_all(your_engine)` to create the tables for all registered models in your database.
4.  **Create and Pass the Session:** Whenever you need to call an `resembl` core function, create a `Session` from your engine and pass it as the `session` argument.

By following this pattern, you can seamlessly integrate `resembl`'s functionality into any application while maintaining full control over the database.
