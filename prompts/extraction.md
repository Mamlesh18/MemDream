Extract facts about the user that are still true next month.{domain}

One fact per memory, third person about "User", self-contained months later:
resolve pronouns, keep names, numbers, dates. Split a message stating several.

Likes and dislikes both count. When unsure, extract — importance ranks a
memory, it never drops one. Never infer past what was said; "I might move" is a
plan, not a move.

`temporalNature` is `current` unless the fact cannot change by nature — a
birthplace or a date of birth. Addresses, phones, jobs, preferences, habits and
orders all change, so they are `current`: `permanent` protects a memory from
ever being corrected.

`sensitive` discards the memory, and is only for payment details, passwords and
government ids. Names, phones, addresses, allergies and dietary needs are
ordinary memories.

Skip anything not about this user. Finding nothing is normal: hasMemory false.
