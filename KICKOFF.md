# Kickoff Prompt

Paste this into any new Claude session (Cowork, web, or Claude Code) to turn it into a
Foundry session. If you install the Foundry plugin, you usually won't need this — the
skills trigger on their own — but it always works, plugin or not.

---

You're operating my Foundry — my project factory.

Repo: `<PASTE YOUR REPO URL HERE — e.g. https://github.com/YOURNAME/foundry.git>`

1. If the repo isn't already in the workspace, clone it. Then read `CLAUDE.md` at its
   root and follow it strictly — especially the end-of-session ritual (state, build
   log, ledger, validate, commit, push).
2. Run the session-start ritual: pull, read `LEDGER.md`, and give me the 3-line brief.
3. Then: **[what you want — e.g. "resume", "intake the doc I attached",
   "build pocket-notes", "status", "upkeep"]**

---

**Private repo?** Append a fine-grained personal access token to the URL:
`https://x-access-token:<TOKEN>@github.com/YOURNAME/foundry.git`
(GitHub → Settings → Developer settings → Fine-grained tokens; scope it to only your
foundry repos, permission "Contents: Read and write". Treat it like a password —
rotate it any time.) If you've connected a GitHub connector to Claude instead, you can
skip the token and just say "use the GitHub connector to reach my foundry repo".

**Tip:** edit this file once with your real URL, then copy the block above from
GitHub's web view whenever you need it.
