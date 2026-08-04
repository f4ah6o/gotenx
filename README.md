# Gotenx — Codex / Claude Code プラグイン + CLI

> **Gotenx v1.4** · Codex対応、運用診断、課金・解析境界の堅牢化

**Gotenx は「複数の AI に意見を出させ、1 つの計画にまとめ、その計画が元の意見を
ちゃんと使っているかを“ごまかせない指標”で測る」ためのパイプラインです。**
Codex と Claude Code のプラグイン、および単体CLIとして動きます。

- 🧩 複数の分析役 CLI（`claude` / `codex` / `opencode`、または v1.3 の固定 4 段階 OpenCode パイプライン）が
  **インサイト（気づき）** を出す（= Panel）
- ⚖️ Judge（裁定役）がそれらを引用しながら 1 つの **計画** に統合する（= Judge）
- 📊 「どのインサイトが計画に採用されたか」を **来歴（provenance）グラフ** から計算して指標化する
- ✅ Eval が指標を検証し、設定変更の **提案（Proposal）** を“劣化させない”範囲でだけ許可する
- 💰 v1.3 ではコスト予算ガードレールと品質/コストの非劣性ベンチマークが加わる

---

## v1.4 で追加されたもの

- **Codexプラグイン対応** — `.codex-plugin/plugin.json` と `skills/gotenx/SKILL.md` を追加。
- **Codex CLIアダプター** — `codex exec --json --sandbox read-only --ephemeral` を標準化し、JSONLの最終メッセージとトークン使用量を解析。
- **`gotenx doctor`** — 必須CLI、実行ファイル、バージョン呼び出し、argvテンプレートをモデル呼び出しなしで診断。
- **プロジェクト別アダプター設定** — `.gotenx/adapters.json` でコマンド、タイムアウト、出力形式を上書き可能。
- **課金保護** — 空タスクを実行前に拒否し、再試行とベンチマークの全呼び出しコストを一度ずつ記録。
- **構造的JSON抽出** — greedy regexを廃止し、サイズ制限付き `JSONDecoder.raw_decode` で配列・オブジェクトを決定的に抽出。

## v1.3 で追加されたもの

Gotenx v1.3 は低コストで高品質なコーディング計画・レビューを狙う。実運用の各 run は
固定の read-only 4 段階シーケンスを通る:

1. `opencode-go/deepseek-v4-flash`（`high`）— scout（偵察）
2. `opencode-go/kimi-k2.7-code` — author（起草）
3. `opencode-go/deepseek-v4-pro`（`high`）— critic（批評）
4. `opencode-go/glm-5.2` — judge（裁定）

各段階は gotenx が振った ID 付きの前段階の出力を受け取る。最終 Judge は使った項目を
必ず引用しなければならず、v1.2 の決定的な来歴指標がそのまま保たれる。OpenCode の
JSON イベントから段階ごとのトークン数・USD コストを計測する。

既存の v2 ポリシー(`.gotenx/policy.json`)は初回実行時に自動で v3 へ移行され、旧設定は
`.gotenx/migrations/` にバックアップされる。

以下は v1.2 の元の設計説明。

## 何の問題を解くのか

AI に「良い計画かどうか」を測らせると、たいてい **テキストの一致**（キーワードが入っているか等）で
判定しがちです。しかしテキスト照合の指標は **Goodhart の法則** — 「指標が目標になると、その指標は
良い指標でなくなる」— に弱く、モデルが指標に合わせて文章を盛るだけで“ごまかせて”しまいます。

Gotenx の答えはシンプルです。**指標は来歴グラフだけから計算する。テキストは一切見ない。**

- Panel が出した各インサイトには、gotenx が **ID**（`<source>:<kind>:<seq>`、例 `claude:insight:001`）を
  振ります。**ID を振るのは gotenx だけで、モデルには振らせません。** だからグラフを偽造できません。
- Judge は計画の各項目に「どのインサイト ID を根拠にしたか」（`source_ids`）を書きます。
- 指標は「参照された ID / 全 ID」のような **純粋な計算** になります（[`gotenx/metrics.py`](gotenx/metrics.py)）。

LLM の主観的判断が必要になる箇所(引用が本当に誠実か＝`provenance_faithful`)は **Eval 層に隔離し、
かつサンプリング** で確認します。これにより指標層は完全に決定的(同じグラフ → 必ず同じ数値)に保たれます。

---

## しくみ(Panel → Judge → Eval → Proposal)

```
タスク
  │
  ▼
┌─────────┐   各分析役がインサイトを出す
│  Panel  │   gotenx が ID を付与 (claude:insight:001 ...)
└────┬────┘   → gotenx/panel.py / gotenx/orchestrator.py (v1.3 staged)
     ▼
┌─────────┐   ID を引用しながら 1 つの計画へ統合
│  Judge  │   各計画項目に source_ids を記録
└────┬────┘   → gotenx/judge.py
     ▼
┌──────────────┐  ID のつながりだけでグラフを構築
│ Provenance   │  (参照された ID / 宙ぶらりんの ID)
│   Graph      │  → gotenx/provenance.py
└──────┬───────┘
       ▼
┌─────────┐   グラフから指標を“純粋計算”
│ Metrics │   → gotenx/metrics.py
└────┬────┘
     ▼
┌─────────┐   ゴールデンケースで反復実行し、保護指標・基準値・誠実性を検証
│  Eval   │   → gotenx/evalrun.py
└────┬────┘
     ▼
┌──────────┐  設定変更の提案を検証 → Eval → 適用(基準値をラチェット)
│ Proposal │  → gotenx/proposal.py
└──────────┘
```

1. **Panel** — 設定された分析役 CLI を headless で呼び、返ってきた各インサイトに ID を振る
   (v1.3 では固定 4 段階 OpenCode パイプラインが scout/author/critic の 3 段を担う)。
2. **Judge** — 全インサイト(ID 付き)を渡し、`source_ids` 付きの計画項目を受け取る。引用先 ID が
   壊れている場合は警告して落とす(黙って残さない)。
3. **Provenance Graph** — Panel の ID と Judge の `source_ids` を結び、参照済み ID / 宙ぶらりんの ID を持つ
   グラフにする。
4. **Metrics** — グラフ上の純粋関数として 5 つの指標を計算(後述)。
5. **Eval / Proposal** — 既存設定を劣化させない範囲でのみ設定変更を許可する(後述のガードレール)。

実行結果は、利用先プロジェクトの `.gotenx/runs/<run_id>/` に保存されます
(このプラグインのリポジトリではなく、`$GOTENX_PROJECT_DIR`、ホスト固有のプロジェクト変数、または cwd 側)。

---

## クイックスタート

LLM が無くても動く **決定的なウォークスルー**(ゴールデンケースの録画フィクスチャを再生)です。
依存は Python 3 標準ライブラリのみ。

```bash
# 1) Claude Codeプラグインとして使う
claude --plugin-dir /path/to/gotenx

# Codexでは、このリポジトリをプラグインとしてインストールすると
# `gotenx` skill が利用可能になる（.codex-plugin/plugin.json）。

# 2) CLIを初期化し、実LLM依存を診断
bin/gotenx init                              # policy.json / adapters.json を配置
bin/gotenx doctor                            # モデルを呼ばずにCLI依存を検証

# 3) LLM不要の決定的な動作確認
bin/gotenx run --replay golden/case-001      # Panel→Judge→指標 を録画フィクスチャで再生
bin/gotenx run "review this repo"            # 位置引数でタスクを渡すことも可能
bin/gotenx eval                              # ゴールデンスイートを反復実行して検証
bin/gotenx status                            # 適用中ポリシー / epoch / 基準値 / 最近の run
python3 scripts/build_benchmark_suite.py     # 60 ケースの品質/コストベンチマークを構築
bin/gotenx benchmark run --suite .gotenx/benchmarks/v1 --resume
```

`run` の出力例(抜粋)— 指標は来歴グラフから計算された純粋な数値です:

```json
{
  "run_id": "…Z-000",
  "metrics": {
    "panel_insight_survival_rate": 1.0,
    "insight_adoption": 1.0,
    "single_attribution_acted_on": 0.666…,
    "observed_diversity_index": 1.0,
    "configured_diversity_index": 1.0
  },
  "metadata": { "panels": ["claude","codex","opencode"], "warnings": [] }
}
```

実際の LLM を使って動かす前に `bin/gotenx doctor` を通し、`--replay` を外します(`bin/gotenx run --task "…"` または
`bin/gotenx run "…"`)。その場合は v1.3 の 4 段階 OpenCode パイプラインが有効なら OpenCode CLI が、
レガシー v2 経路なら `claude` / `codex` / `opencode` CLI が利用可能である必要があります。

---

## スラッシュコマンド

スラッシュコマンドはすべて `bin/gotenx`(決定的なコア)の薄いラッパーです。

| コマンド | 内容 |
|---|---|
| `/gotenx:init` | `.gotenx/` を作成し、正準 `policy.json` と `adapters.json` を設置、基準値を初期化 |
| `/gotenx:doctor` | 必須エージェントCLIとアダプター設定を、モデル呼び出しなしで診断 |
| `/gotenx:run <task>` | 4 段階 OpenCode 熟考サイクルと計測済み指標を実行(`--replay <dir>` でフィクスチャ再生) |
| `/gotenx:eval` | ゴールデンスイートを実行(反復実行・ケース別の合否・実測値) |
| `/gotenx:benchmark` | 60 ケースの品質/コストベンチマークを取得・レポート |
| `/gotenx:propose <p.json>` | 提案を検証し、候補となる実効設定を Eval にかける |
| `/gotenx:apply <p.json>` | 検証と Eval を通った提案を適用し、基準値をラチェット |
| `/gotenx:status` | 適用中ポリシー・epoch・基準値・最近の run・使用量予算を表示 |

---

## 概念 / 用語集

README を読み進めるための最小限の用語です。仕様の厳密な定義は [`docs/`](docs/) を参照。

| 用語 | 平たく言うと |
|---|---|
| **来歴グラフ(provenance graph)** | 「Panel のインサイト ID」と「Judge が引用した `source_ids`」を結んだ ID のつながり。すべての指標はこれだけから計算される([`gotenx/provenance.py`](gotenx/provenance.py))。 |
| **Goodhart 耐性(hardened)** | 指標を目標にしても壊れにくいこと。ID を gotenx が振り、テキストではなくグラフで測ることで、文章を盛るだけのごまかしを防ぐ。 |
| **保護指標(protected metric)** | 提案の合否を決める“門番”の指標。基準値(floor)を持ち、それを下回る変更は通さない。 |
| **構造不変条件(structural invariant)** | 提案が決して弱めてはならない名前付きの取り決め(例 `judge_provenance_policy` / `no_proposal_metric_promotion`)。 |
| **基準値ラチェット(baseline ratchet)** | 適用が成功するたびに基準値を `実測の最小値 − 許容幅` へ更新する。単調増加で、決して下がらない。ノイズで門番が暴れないようヒステリシスを持たせる。 |
| **epoch** | 適用ごとに +1 される世代番号。古い epoch を前提に評価された提案は無効化され、再評価が要る。 |
| **future_candidate_metric** | まだ正式採用していない“候補”の指標。非保護・非ゲートで、オプティマイザが勝手に保護指標へ昇格させることはできない(昇格は仕様改訂でのみ)。 |
| **provenance_faithful** | Judge の引用が誠実かを問う、Eval 層だけのサンプリング検査。LLM 判断が入る唯一の箇所で、しきい値(既定 0.90)で確認する。 |

---

## 指標(すべて決定的)

| 指標 | 意味 | 備考 |
|---|---|---|
| `panel_insight_survival_rate` | 参照された Panel ID / 全 Panel ID | **保護指標**(基準値あり) |
| `single_attribution_acted_on` | `source_id` をちょうど 1 つだけ持つ Judge 項目の割合 | |
| `insight_adoption` | 1 つ以上の Panel インサイトに根拠を持つ Judge 項目の割合 | |
| `observed_diversity_index` | 参照されたインサイトに現れた発信元の多様性(実測) | 保護しない |
| `configured_diversity_index` | 設定された Panel 発信元の多様性(静的設定ベース) | **保護指標** |

> 多様性指標は「実測(observed)」と「設定(configured)」を分けてあり、保護するのは設定側だけです。
> 実測側を保護対象にすることは禁止されています。

---

## ガードレール

提案(設定変更)は、適用される前に複数の門を通ります。

- **保護ポリシーキー / 構造不変条件** — これらに触れる提案は Eval 以前に却下される。
- **`no_proposal_metric_promotion`** — オプティマイザは候補指標を保護指標へ昇格できない。昇格は
  仕様改訂のみが行える。
- **基準値ラチェット** — floor = `実測 − 許容幅`。単調増加で下がらない。
- **逐次適用** — 適用ごとに epoch が進み、古い epoch で評価された提案は無効になる。

サンプルの提案は [`examples/`](examples/) にあります([受理される例](examples/proposal-accepted.json) /
[昇格を試みて却下される例](examples/proposal-rejected-promotion.json) /
[保護キーに触れて却下される例](examples/proposal-rejected-protected-key.json))。

---

## コストガードレール(v1.3)

- ローカル Gotenx 使用量ウィンドウ: `$12 / 5h`、`$30 / 7d`、`$60 / 30d`。
- 新しい run のたびに `$0.10` を予約し、超過見込みなら実行をブロックする。
- 使用量計測を伴わない実出力は拒否される。
- OpenCode Go の **Use balance** を無効化し、有料 Zen クレジットへのフォールバックを防ぐこと。
- 受け入れベンチマークは、候補コストが Opus 4.6 high と GPT-5.5 high の平均コストの 65% 以下で
  あることを要求する。

ローカル台帳は Gotenx 経由でない OpenCode Go の使用量を観測できない。

## 品質ベンチマーク(v1.3)

`scripts/build_benchmark_suite.py` は 60 ケースの均衡セットを作る: 30 プラン + 30 レビュー、
30 件の固定 public PR + 30 件の合成タスク、Python/TypeScript/Go/Rust/Swift/MoonBit 各 10 件。
public 記録には URL・マージコミット・ライセンスが含まれる。

GPT-5.5 high が候補 vs Opus を採点し、Opus 4.6 high が候補 vs GPT を採点する。候補の提示順は
決定的にランダム化される。固定シードの 10,000 サンプル bootstrap で、選定した 10 ポイントの
非劣性マージンに対応する 95% 下側スコア境界 ≥0.40 を満たす必要がある。

---

## ディレクトリ構成

```
.claude-plugin/plugin.json   Claude Codeプラグインのマニフェスト
.codex-plugin/plugin.json    Codexプラグインのマニフェスト
skills/gotenx/SKILL.md       Codex向けの実行ワークフロー
commands/*.md                Claude Codeスラッシュコマンド定義(bin/gotenx を呼ぶ)
agents/gotenx-judge.md        Judge サブエージェント(来歴に忠実な統合)
hooks/hooks.json              PostToolUse フック(policy.json 編集時に検証)
bin/gotenx                    決定的な CLI 本体
bin/gotenx-validate-policy    フック本体:編集された policy.json を検証
gotenx/                       Python パッケージ(ids, provenance, metrics, eval, orchestrator, benchmark …)
config/policy.json            正準ポリシー(schema: gotenx.config.policy.v3)
config/adapters.json          Claude/Codex/OpenCodeの正準CLIアダプター
benchmarks/v1/sources.json    固定の public PR ベンチマークソース
scripts/build_benchmark_suite.py  30 public + 30 合成ケースを構築
golden/case-*/                検証用のゴールデンケース(再生フィクスチャ)
examples/*.json                サンプル提案(受理 / 却下)
docs/                          仕様(spec.md と patch1-3.md)
tests/                         unittest スイート
```

利用先プロジェクトの **実行時の状態**は `<project>/.gotenx/` に置かれます
(適用済み `policy.json`、`adapters.json`、`baseline.json`、使用量台帳、ベンチマークのチェックポイント、
`runs/<run_id>/`、`proposals/`)。既存の v2 状態は v3 への自動移行前に `.gotenx/migrations/`
にバックアップされる。このプラグインのリポジトリ自体には書き込みません。

---

## テスト

```bash
python3 -m unittest discover -s tests
```

ビルドや lint のステップはありません(標準ライブラリのみの Python + Markdown のプラグイン資産)。

---

## 仕様と決定性について

Gotenx は **仕様 v1.2「Frozen Baseline」**(凍結ベースライン、P1–P24)の実装を土台に、v1.3 で
4 段階 OpenCode パイプライン・コスト予算・品質ベンチマークを追加したものです。凍結とは
「このバージョンは閉じて不変」という意味で、「全ての疑問が解決済み」という意味ではありません。
下記の未解決項目は、凍結ベースラインが認識している **限界** であって、v1.2 に追加で当てるべき欠陥
ではありません。

仕様の全文と各ポイント(P◯)の定義は次を参照してください:

- [`docs/spec.md`](docs/spec.md) — 統合版(P22–P24 を含む完全版)
- [`docs/patch1.md`](docs/patch1.md) — P1–P8
- [`docs/patch2.md`](docs/patch2.md) — P9–P21
- [`docs/patch3.md`](docs/patch3.md) — 凍結デルタ
- [`docs/patch4.md`](docs/patch4.md) — Codex対応・アダプター診断・課金/解析境界の堅牢化

### 既知の未解決項目(持ち越し、ブロッカーではない)

- **`semantic_minority_survival_rate`** — 定義は v1.3 以降へ持ち越し。現状は決定的な floor である
  `panel_insight_survival_rate` で近似している。非保護・非ゲート・未批准の `future_candidate_metric`
  として保持(P22)。
- **来歴の誠実性はサンプリングであり網羅ではない**(P23)。Judge(LLM)が `source_ids` を書く以上、
  追加の構造的強制層なしに引用の誠実性を完全に決定的へ保証する方法は無い。この上限を引き上げるのは
  v1.3 以降のアーキテクチャ課題で、凍結ベースラインの範囲外。

```
決定性: 9.8 / 10
  0.2 の差は、来歴の誠実性がサンプリングであること。
  これは仕様不足ではなく、構造上の必然による。
```
