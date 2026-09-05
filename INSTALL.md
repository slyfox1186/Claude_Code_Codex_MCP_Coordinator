# INSTALL

On a Linux server, run:

```bash
git clone https://github.com/slyfox1186/Claude_Code_Codex_MCP_Coordinator.git
cd Claude_Code_Codex_MCP_Coordinator
./setup.sh
```

Follow the prompts. Setup explains each change and asks for consent before it:

- If Conda is detected, setup creates a dedicated environment named `agent-duet`. Setup never installs Conda or changes `base` or any existing environment.
- Without Conda, setup uses the default Python 3.13+ only to create a private Agent Duet
  environment. It does not install packages into system Python.
- If Claude Code or Codex is missing, setup offers their official installer and waits for `y`.
- Setup offers sign-in, then asks whether to make a throwaway demo. Answer `n` to enter the
  relative or full path to your real Git repository instead.

Check the finished installation:

```bash
./setup.sh check
```

Requirements: Linux, Git, and Python 3.13 or newer. `curl` or `wget` is needed only when a
provider CLI must be downloaded.

If you decline a required change, nothing from that step is installed; rerun `./setup.sh` when
ready. If sign-in was skipped, run the command setup printed. For deeper diagnostics, run
`agent-duet doctor`.

Agent sessions receive broad access to your machine. Read [SECURITY.md](SECURITY.md) before use.
