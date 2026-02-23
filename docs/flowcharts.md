# asmatch Flowcharts

This document contains Mermaid charts illustrating the core workflows of the `asmatch` tool.

## Add Snippet Flow

This chart describes the process of adding a new snippet to the database.

```mermaid
graph LR
    subgraph "Input"
        A[asmatch add 'name' 'code']
    end

    subgraph "Processing"
        B{Normalize Code & Generate Tokens}
        C{Calculate SHA256 Checksum of Normalized Code}
        D{Snippet with Checksum Exists in DB?}
    end

    subgraph "Existing Snippet Path"
        D -- Yes --> E[Add New Name as Alias to Existing Snippet]
    end

    subgraph "New Snippet Path"
        D -- No --> F{Generate MinHash from Tokens}
        F --> G[Store Snippet in DB: Checksum, Names, Code, MinHash]
        G --> H{Invalidate LSH Cache}
    end

    subgraph "Output"
        E --> I[Show Confirmation]
        H --> I
    end

    A --> B --> C --> D
```

## Find Matches Flow

This chart illustrates the process of finding similar snippets.

```mermaid
graph LR
    subgraph "Input"
        A[asmatch find --query 'code']
    end

    subgraph "Query Processing"
        B{Normalize Query & Generate MinHash}
    end

    subgraph "LSH Caching"
        C{Load LSH Cache from Disk}
        C -- Cache Miss/Stale --> D{Build LSH Index from All Snippet MinHashes in DB}
        D --> E{Save New LSH Index to Cache on Disk}
        E --> F[LSH Index Ready]
        C -- Cache Hit --> F
    end

    subgraph "Candidate Retrieval"
        F --> G{Query LSH Index with Query MinHash}
        G --> H{Retrieve Candidate Snippets from DB via Checksum}
    end

    subgraph "Ranking & Output"
        H --> I{Rank Candidates with Levenshtein Similarity Score}
        I --> J[Display Top N Matches to User]
    end

    A --> B --> C
```

## Import Snippets Flow

This chart describes the bulk import process.

```mermaid
graph LR
    subgraph "Input"
        A[asmatch import 'directory']
    end

    subgraph "Confirmation"
        B{--force flag set?}
        B -- No --> C[Prompt User for Confirmation]
        C --> D{User Confirmed?}
        D -- No --> E[Abort]
        B -- Yes --> F[Proceed]
        D -- Yes --> F
    end

    subgraph "File Discovery"
        F --> G["Glob for *.asm and *.txt files recursively"]
    end

    subgraph "Per-File Processing"
        G --> H[Read File Content]
        H --> I{Calculate Checksum}
        I --> J{Snippet Already Exists?}
        J -- Yes --> K[Add Filename as Alias]
        J -- No --> L[Add New Snippet to DB]
        L --> M[Increment New Count]
    end

    subgraph "Output"
        K --> N[Report Import Statistics]
        M --> N
    end

    A --> B
```

## Export Snippets Flow

This chart describes the snippet export process.

```mermaid
graph LR
    subgraph "Input"
        A[asmatch export 'directory']
    end

    subgraph "Confirmation"
        B{--force flag set?}
        B -- No --> C[Prompt User for Confirmation]
        C --> D{User Confirmed?}
        D -- No --> E[Abort]
        B -- Yes --> F[Proceed]
        D -- Yes --> F
    end

    subgraph "Export Processing"
        F --> G[Create Export Directory]
        G --> H[Load All Snippets from DB]
    end

    subgraph "Per-Snippet Processing"
        H --> I["Sanitize Primary Name for Filesystem Safety"]
        I --> J{Resolved Path Within Export Dir?}
        J -- No --> K[Skip and Warn]
        J -- Yes --> L["Write Code to 'name.asm' File"]
    end

    subgraph "Output"
        L --> M[Report Export Statistics]
        K --> M
    end

    A --> B
```
