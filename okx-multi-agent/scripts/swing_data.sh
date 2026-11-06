#!/usr/bin/env bash
# 在 GitHub Actions 里恢复 / 发布波段运行状态。
# 数据放在独立分支 swing-data，只含交易日志和 state，不含密钥。
# 每次提交都算仓库活动，避免定时工作流因 60 天无活动被 GitHub 关掉。
#
#   bash okx-multi-agent/scripts/swing_data.sh restore
#   bash okx-multi-agent/scripts/swing_data.sh publish
#
# 环境变量：
#   SWING_DATA_DIR  默认 <repo>/swing-data
#   SWING_DATA_BRANCH  默认 swing-data
set -euo pipefail

cmd="${1:-}"
repo_root="$(git rev-parse --show-toplevel)"
data_dir="${SWING_DATA_DIR:-$repo_root/swing-data}"
branch="${SWING_DATA_BRANCH:-swing-data}"
allow=(state.json trades.csv equity.csv equity.json decisions.jsonl universe.json last_run.json README.md)

cd "$repo_root"

write_readme() {
  cat > "$data_dir/README.md" << 'EOF'
# swing-data

OKX 模拟盘 `swing_combo` 的运行档案（MA-2.0）。由 GitHub Actions 写入，不要在这个分支上跑机器人。

包含：`state.json`（入场家族、开仓时间、峰值权益、冷却、账户停机）、`trades.csv`、`equity.csv`、`equity.json`、`decisions.jsonl`、`universe.json`、`last_run.json`。

这里没有 API 密钥。分析多日结果时看 `equity.csv` 和 `trades.csv`。
EOF
}

write_manifest_if_missing() {
  if [[ -f "$data_dir/last_run.json" ]]; then
    return
  fi
  mkdir -p "$data_dir"
  python3 - "$data_dir/last_run.json" << 'PY'
import json, os, sys, time
path = sys.argv[1]
json.dump({
    "run_id": os.getenv("GITHUB_RUN_ID"),
    "run_attempt": os.getenv("GITHUB_RUN_ATTEMPT"),
    "sha": os.getenv("GITHUB_SHA"),
    "ref": os.getenv("GITHUB_REF"),
    "event": os.getenv("GITHUB_EVENT_NAME"),
    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
PY
}

refuse_secrets() {
  local src="$1"
  if grep -R -E '^(OKX_API_KEY|OKX_SECRET_KEY|OKX_PASSPHRASE)=[^[:space:]]+' "$src" >/dev/null 2>&1; then
    echo "拒绝发布：swing-data 里像是写进了 API 密钥" >&2
    exit 1
  fi
}

restore() {
  mkdir -p "$data_dir"
  git fetch origin "$branch" || true
  if git rev-parse --verify "origin/$branch" >/dev/null 2>&1; then
    if git checkout "origin/$branch" -- swing-data; then
      echo "已从 origin/$branch 恢复 swing-data"
    else
      echo "origin/$branch 上还没有 swing-data 目录，本轮从空档案开始"
    fi
  else
    echo "还没有 origin/$branch，本轮从空档案开始（交易所持仓仍会通过对账接上）"
  fi
}

publish() {
  mkdir -p "$data_dir"
  write_readme
  write_manifest_if_missing
  local payload
  payload="$(mktemp -d)"
  mkdir -p "$payload/swing-data"
  local f
  for f in "${allow[@]}"; do
    if [[ -f "$data_dir/$f" ]]; then
      cp "$data_dir/$f" "$payload/swing-data/$f"
    fi
  done
  refuse_secrets "$payload"

  git fetch origin "$branch" || true
  local wt_parent wt
  wt_parent="$(mktemp -d)"
  wt="$wt_parent/wt"
  cleanup() {
    trap - RETURN
    if [[ -n "${wt:-}" ]]; then
      git worktree remove --force "$wt" >/dev/null 2>&1 || true
    fi
    rm -rf "${payload:-}" "${wt_parent:-}"
  }
  trap cleanup RETURN

  if git rev-parse --verify "origin/$branch" >/dev/null 2>&1; then
    git worktree add --detach "$wt" "origin/$branch"
  else
    git worktree add --detach "$wt" HEAD
    git -C "$wt" checkout --orphan "$branch"
    git -C "$wt" rm -rf --cached . >/dev/null 2>&1 || true
    git -C "$wt" clean -fd >/dev/null 2>&1 || true
  fi
  mkdir -p "$wt/swing-data"
  # 只保留白名单，避免把别的文件带上数据分支。
  find "$wt/swing-data" -mindepth 1 -maxdepth 1 -type f -delete
  cp -a "$payload/swing-data/." "$wt/swing-data/"
  git -C "$wt" add -f swing-data
  if git -C "$wt" diff --cached --quiet; then
    echo "swing-data 没有变化，跳过提交"
    return
  fi
  git -C "$wt" -c user.email="github-actions[bot]@users.noreply.github.com" \
    -c user.name="github-actions[bot]" \
    commit -m "swing-data: ${GITHUB_RUN_ID:-local} ${GITHUB_SHA:-manual}"
  git -C "$wt" push origin "HEAD:$branch"
  echo "已推送 $branch"
}

case "$cmd" in
  restore) restore ;;
  publish) publish ;;
  *)
    echo "用法: $0 restore|publish" >&2
    exit 2
    ;;
esac
