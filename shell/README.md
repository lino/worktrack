# Worktrack (`wt`) Cross-Shell Prompt Integrations

Low-latency, non-blocking shell prompt plugins querying `worktrackd` via `wt status --porcelain` (fast-failing in `< 15ms` with exit code `0` and empty output whenever idle or offline).

## 1. Starship (`starship.rs`)

Append the `[custom.worktrack]` table from `shell/starship.toml` to `"${XDG_CONFIG_HOME:-$HOME/.config}/starship.toml"`:

```toml
[custom.worktrack]
description = "Active Worktrack Interval and daily total"
command = "wt status --porcelain"
when = "command -v wt"
style = "bold cyan"
format = "[$output]($style)"
require_repo = false
ignore_timeout = true
```

## 2. Oh My Zsh / Zsh (`worktrack.plugin.zsh`)

1. Copy `shell/worktrack.plugin.zsh` into `"${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/worktrack/worktrack.plugin.zsh"`.
1. Add `worktrack` to `plugins=(...)` in `"$HOME/.zshrc"` and reference `$(worktrack_prompt_info)` in `RPROMPT`.
1. Customize optional delimiters via `WORKTRACK_ZSH_PREFIX` and `WORKTRACK_ZSH_SUFFIX`.

## 3. PowerShell (`WorktrackPrompt.psm1`)

Import `shell/WorktrackPrompt.psm1` inside `$PROFILE` (resolved under `$HOME` or `$env:LOCALAPPDATA`) and prepend `Get-WorktrackPromptSegment` inside `function prompt`:

```powershell
Import-Module "$env:WORKTRACK_SHELL_DIR/WorktrackPrompt.psm1"

function prompt {
    $wt = Get-WorktrackPromptSegment
    if ($wt) { "$wt PS $($executionContext.SessionState.Path.CurrentLocation)> " }
    else { "PS $($executionContext.SessionState.Path.CurrentLocation)> " }
}
```
