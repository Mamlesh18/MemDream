Decide whether a new memory duplicates or enriches existing memories and should
be consolidated. Return MERGE or NOOP only — another stage handles ADD and
SUPERSEDE.{domain}

MERGE when the new memory duplicates an existing one, restates it in other
words, or adds detail. Return the most complete phrasing as memory.content,
losing no detail either input had. Same topic is not the same fact — two
preferences stay two memories. Never merge different subjects. Gain nothing,
return NOOP.
targetMemoryIds: the memories folded in, ids exactly as given.

Example — existing "User owns a dog named Rex.", new "Rex is three years old
and a Golden Retriever." Canonical: "User owns a three-year-old Golden Retriever
named Rex." Both inputs survive in it.

You recommend; the backend executes, drops ids it did not give you, and
deletes nothing. Give a `reason` naming the facts.
