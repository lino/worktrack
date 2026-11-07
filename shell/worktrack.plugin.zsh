# Worktrack (`wt`) prompt plugin for oh-my-zsh / Zsh
# Install into "${ZSH_CUSTOM:-$HOME/.oh-my-zsh/custom}/plugins/worktrack/worktrack.plugin.zsh"
# and add `worktrack` to `plugins=(...)` in "$HOME/.zshrc", then include
# `$(worktrack_prompt_info)` inside `RPROMPT` or `PROMPT`.

typeset -g WORKTRACK_ZSH_PREFIX="${WORKTRACK_ZSH_PREFIX:-[wt: }"
typeset -g WORKTRACK_ZSH_SUFFIX="${WORKTRACK_ZSH_SUFFIX:-]}"

worktrack_prompt_info() {
  (( $+commands[wt] )) || return 0
  local segment
  segment="$(command wt status --porcelain 2>/dev/null)" || return 0
  if [[ -n "$segment" ]]; then
    printf '%s%s%s' "$WORKTRACK_ZSH_PREFIX" "$segment" "$WORKTRACK_ZSH_SUFFIX"
  fi
  return 0
}
