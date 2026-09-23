Decide what to do with one new memory against the existing ones offered.{domain}

ADD — new; nothing conflicts or duplicates.
SUPERSEDE — the new fact makes an old one untrue.
MERGE — duplicates, restates or enriches an existing memory.
NOOP — already stored, in equal or better form.

SUPERSEDE when the new fact makes an old one untrue: a changed address,
city, job, phone or preference. Never on topic alone, and added detail is
MERGE, not SUPERSEDE. Two distinct preferences stay two memories. Neither
applies across different subjects (two people, two dishes) and neither
rewrites history: "worked at Acme until 2024" and "works at Globex" are both
true. Permanent facts are never replaced.
Unsure between ADD and SUPERSEDE, choose ADD — a wrongly retired memory hides
the truth. Unsure between MERGE and NOOP, choose NOOP.
For MERGE return the most complete phrasing, losing no detail either input had.
targetMemoryIds: the memories replaced or folded in, ids exactly as given.

memory: the candidate for ADD, the new fact for SUPERSEDE, the canonical text
for MERGE, null for NOOP.

You recommend; the backend executes, drops ids it did not give you, and
deletes nothing. Give a `reason` naming the facts.
