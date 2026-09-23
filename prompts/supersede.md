Decide whether a new memory contradicts existing memories and replaces them.
Return SUPERSEDE or NOOP only — another stage handles ADD and MERGE.{domain}

SUPERSEDE only when the new fact makes an old one untrue — not because it is
newer, related, or on the same topic. Added detail is MERGE. Different subjects
never conflict (two people, two dishes). Historical facts survive: "worked at
Acme until 2024" and "works at Globex" are both true. Permanent facts are never
replaced by current ones.
targetMemoryIds: every memory replaced and only those, ids exactly as given.
supersededValidUntil: when the old fact stopped being true, if knowable.

You recommend; the backend executes, drops ids it did not give you, and
deletes nothing. Give a `reason` naming the facts.
