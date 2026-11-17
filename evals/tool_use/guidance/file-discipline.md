# File-tool discipline experiment

- If a module path is uncertain, locate it with find, ls or grep before reading it. Do not guess a conventional module name.
- Use the current file and the bounds reported by read; historical source-map line numbers are not current offsets.
- After a formatter or shell command changes a file, reread it before using edit or overwriting it. Do not reuse text from the pre-format read.
- Give edit enough surrounding context to match exactly once. After a rejected edit, inspect the current text before retrying.
