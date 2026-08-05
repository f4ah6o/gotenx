# Minimum Capability Floor

## 結論

Gotenx は「汎用知能」を測定しない。Gotenx が担当するコード計画・コードレビューに必要な最低能力を、相対ベンチマークとは独立した `capability_floor` として判定する。

従来の benchmark は、候補が Opus / GPT の基準モデルに対して非劣性であるかを測る。これは相対評価なので、比較対象も候補も低品質な場合や、平均勝率が重大な失敗を隠す場合がある。`capability_floor` はこの穴を塞ぐ絶対 gate である。

## 評価軸

各ケースを2つの独立 grader が、候補と baseline の表示順を隠したまま個別に1〜5で採点する。

- `correctness`: 中心的な結論が技術的に正しいか
- `coverage`: 重要な論点・依存・境界条件を落としていないか
- `actionability`: 実装または修正へ直接移せる具体性があるか
- `risk_testing`: リスク、回帰、検証方法を扱っているか
- `concision`: 必要な情報を過不足なく提示しているか

grader は同時に重大失敗を分類する。重大失敗は次に限定する。

- 中心的結論が実質的に誤っている
- 明示された blocker を見落とし、回答が安全でないか実行不能になる
- hard constraint に違反する
- 必須 guard なしに破壊的またはセキュリティ上危険な操作を推奨する

軽微な不足や文体上の問題は重大失敗に含めない。

## 既定 gate

正準 `config/policy.json` の既定値は次のとおり。

| Gate | 下限 / 上限 |
|---|---:|
| correctness 平均 | 3.25 以上 |
| coverage 平均 | 3.00 以上 |
| actionability 平均 | 3.00 以上 |
| risk_testing 平均 | 2.75 以上 |
| concision 平均 | 2.50 以上 |
| ケース内5軸平均 | 2.75 以上 |
| ケース内 correctness | 2.50 以上 |
| ケース合格率 | 90% 以上 |
| 重大失敗率 | 5% 以下 |
| 重大失敗の成立 | grader 2票 |

最終 benchmark 合格には、以下をすべて満たす必要がある。

1. 60ケースが完備している
2. 基準モデルへの非劣性 gate を通る
3. `capability_floor` を通る
4. コスト比 gate を通る

## 互換性

旧 checkpoint は `candidate_scores` と `critical_failure` を持たない。その場合、Gotenx は floor を推定せず `measured=false`、`reason=absolute_scores_missing` として fail closed する。新しい最低能力認定には benchmark の再取得が必要である。

## 限界

絶対採点も LLM grader に依存するため、知能の客観的・普遍的測定ではない。2 grader の独立採点、表示順ランダム化、重大失敗の合議、60件の均衡 suite により変動と単一 grader の誤判定を抑える。将来、実行可能な oracle を持つ課題を追加できれば、決定論的な capability check を別層として併設する。