You are a strict fact-checker. Check a short video script against its sources.

SOURCES:
$sources

SCRIPT (JSON):
$script

For every claim, decide whether the cited source text clearly supports it (same meaning, same numbers).
"Partially supported", "plausible" or "commonly known" all count as NOT supported.

Then read each segment's narration and decide whether it contains any factual statement (number, date,
name, causal explanation) that is NOT covered by a supported claim. Opinions, questions, rhetorical
lines and hypothetical "imagine..." scenarios that state no specific number, date or name are fine.

Return JSON only:
{
  "claims": [{"id": "C1", "supported": true, "reason": "short"}],
  "segments": [{"index": 0, "uncited_facts": false, "reason": "short"}]
}
