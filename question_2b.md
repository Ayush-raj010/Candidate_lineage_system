# Question 2b: Review the AI's work

The function from the brief:

```js
function matchCandidates(candidates, requiredSkills) {
  let matches = [];
  for (i = 0; i <= candidates.length; i++) {
    const c = candidates[i];
    const score = c.skills.filter(s => requiredSkills.includes(s));
    if (score.length > 0) {
      matches.push({ name: c.name, score: score });
    }
  }
  return matches.sort((a, b) => a.score - b.score);
}
```

I checked each finding below by running the original in Node, with these three candidates and
`requiredSkills = ["Python", "Docker", "AWS"]`:

```js
[{ name: "A", skills: ["Python"] },
 { name: "B", skills: ["Python", "Docker", "AWS"] },
 { name: "C", skills: ["python", "Python", "Python"] }]
```

## 1. Bugs

### Bug 1: Off-by-one loop bound crashes on every call

`i <= candidates.length` runs one step past the end. On the last step `candidates[i]` is
`undefined`, so `c.skills` throws:

```
TypeError: Cannot read properties of undefined (reading 'skills')
```

This happens for **every** input, even an empty array, because `candidates[0]` is `undefined`
there too. The function never returns a result.

### Bug 2: The sort compares arrays, so it doesn't sort

`score` is the *array* of matched skills, not a number. `a.score - b.score` subtracts two
arrays, which gives `NaN` (`["Python"] - ["Python", "Docker"]` → `NaN`). A comparator that
returns `NaN` leaves the order undefined. In practice the results come back in input order.

It's also the wrong direction. Even with numbers, `a - b` sorts ascending, so the **worst**
match would come first. With only the loop bound fixed, the run above returned A (1 match),
B (3 matches), C, in that order: the best candidate wasn't on top.

### Bug 3: Undeclared loop variable

`i` is never declared. Outside strict mode it silently becomes a **global** (`globalThis.i`
was `3` after the call), which other code can overwrite or read. In strict mode, which ES
modules and classes always use, it throws `ReferenceError: i is not defined`.

### Bug 4: Duplicate tags inflate the score, and matching is case-sensitive

`filter` counts every matching tag, so candidate C, tagged `"Python"` twice, scored **2**
from a single skill. Meanwhile `includes` is exact, so C's `"python"` didn't match at all.
Messy tag data (duplicates, different casing) changes the ranking in both directions.

### Design smells

- **`score` is the wrong type for its name.** It holds the matched skills, not a score. That
  mismatch is exactly what caused Bug 2. Return a number as `score` and the list as
  `matchedSkills`.
- **Results carry only `name`.** Names aren't unique, so two "Priya Sharma"s can't be told
  apart, and the caller can't look the candidate up. Return an `id`.
- **`requiredSkills.includes` inside `filter`** is a linear search per tag: O(s·q) work per
  candidate. A `Set` makes each check O(1).
- **`let matches`** is never reassigned (it should be `const`), and `c` is a vague name.
  These are minor, but they're signs the code wasn't reviewed.

## 2. Fixed version

```js
function normalizeSkill(skill) {
  return String(skill).trim().replace(/\s+/g, " ").toLowerCase();
}

/**
 * Rank candidates by how many distinct required skills they have, best first.
 * Ties keep input order (Array.prototype.sort is stable).
 */
function matchCandidates(candidates, requiredSkills) {
  const required = new Set(requiredSkills.map(normalizeSkill));
  const matches = [];

  for (const candidate of candidates) {        // no index, so no off-by-one and no global
    const seen = new Set();
    const matchedSkills = [];
    for (const skill of candidate.skills ?? []) {
      const key = normalizeSkill(skill);
      if (required.has(key) && !seen.has(key)) {   // each skill counts once, any casing
        seen.add(key);
        matchedSkills.push(skill);
      }
    }
    if (matchedSkills.length > 0) {
      matches.push({
        id: candidate.id,
        name: candidate.name,
        score: matchedSkills.length,               // a number, so the comparator works
        matchedSkills,
      });
    }
  }

  return matches.sort((a, b) => b.score - a.score); // descending: best match first
}
```

On the same input, the fixed version returns B (score 3), then A and C (score 1 each, in
input order). C now scores 1, not 2, and its `"python"` tag matches.

What changed:

| Problem | Fix |
|---|---|
| `i <= length` crash | `for...of`, so there's no index to get wrong |
| Undeclared `i` | Gone with the index; `const` everywhere else |
| Comparing arrays | `score` is a number; comparator is `b.score - a.score` |
| Ascending order | Descending, best first |
| Duplicates counted twice | `seen` set: each skill counts once |
| Case/spacing mismatches | `normalizeSkill` on both sides |
| Only `name` returned | Returns `id`, `name`, `score`, `matchedSkills` |
| O(q) `includes` per tag | `Set` lookup, O(1) |
| Missing `skills` crashes | `candidate.skills ?? []` |

Complexity is O(C·s + M log M) for C candidates with s tags each and M matches. That's fine
for a single call. For 100,000 candidates and repeated searches, I'd use the inverted index from
[question_2a.md](question_2a.md), which only reads candidates that have a requested skill.

## 3. What a developer who didn't read the output would have missed

A developer who skimmed this would see a familiar loop-filter-sort shape and assume it works.
But it crashes on every call, so it can't have been run even once. If the off-by-one were
patched in a hurry, the remaining bugs fail silently: the array subtraction yields `NaN`, so the
"ranking" is just input order. The score counts duplicate tags and misses differently-cased ones,
and `i` leaks into global scope. None of these throw an error. Recruiters would get a plausible
list with the wrong candidates on top, and nothing would tell anyone it was wrong.
