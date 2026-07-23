# Serena Code Map

> [!NOTE]
> **Unofficial fork of [oraios/serena](https://github.com/oraios/serena).**
> Serena 本体の機能・セットアップ・設定については **[本家 Serena の README](https://github.com/oraios/serena#readme)** を参照してください。
> 本リポジトリの README では、この fork で追加した機能とその導入方法のみを説明します。
> Oraios AI とは無関係であり、公式プロダクトではありません。

## この fork で追加した機能

コーディングエージェントのクレジット消費(初見リポジトリの探索・呼び出し関係の反復調査・毎セッションの同じ構造の再調査)を抑えるための、**静的コードマップ生成 CLI** を追加しています。

```bash
serena project export-code-map [PROJECT]
```

Serena が LSP から取得できる情報(document symbols / hover / call hierarchy / type hierarchy)だけを使い、LLM を一切使わずにローカルで次の成果物を生成します。

```text
.serena/code-map/
├── overview.md        # 小さな起動時マップ(coverage、主要クラス、型依存、call root候補)
├── manifest.json      # 解析範囲・言語サーバーごとの対応状況
├── symbols.jsonl      # 全シンボル(ID・シグネチャ・ドキュメント・位置)
├── edges.jsonl        # 関係グラフ(CONTAINS / CALLS / TYPE_SUPERTYPE / CLASS_DEPENDS_ON)
├── diagnostics.jsonl  # 生成時の警告・エラー
├── AGENTS_SNIPPET.md  # AGENTS.md / CLAUDE.md に貼るエージェント向け案内
└── modules/           # ソースファイル単位の詳細 Markdown(Calls / Called by 付き)
    └── <relative-source-path>.md
```

### 特徴

- **決定的な出力** — 同じコードからは同じバイト列を生成。内容が変わらないファイルは再書き込みしないため、Git 差分やエージェントの prompt cache を汚しません
- **LLM 不使用** — 解析は LSP とローカル処理のみ。コメントがない箇所で役割を捏造しません
- **既存の Serena を壊さない** — 新しい MCP ツールは追加せず、既存の symbol tools は従来どおり動作します
- **JavaDoc / docstring 保持** — hover から取得した signature・`@param`・`@return` 等を `signature` / `documentation` として構造化保存
- **Call Hierarchy 非対応の言語サーバーでも動作** — 対応状況は `manifest.json` の coverage に記録され、export 自体は成功します

### 主なオプション

```text
--output PATH                    出力先(デフォルト: <project>/.serena/code-map)
--include-docs / --no-include-docs
--include-calls / --no-include-calls
--include-type-hierarchy / --no-include-type-hierarchy
--hover-budget-seconds FLOAT     hover の時間予算(0 = 無制限、デフォルト)
--strict                         未対応 capability やエラーを非ゼロ exit にする
--overview-max-chars INTEGER     overview.md のサイズ上限(デフォルト: 32768)
```

## 導入方法

### 1. コードマップの生成

インストール不要で `uvx` から直接実行できます([uv](https://docs.astral.sh/uv/) が必要):

```bash
uvx --from git+https://github.com/TakumaAida/serena-code-map serena project export-code-map /path/to/your/project
```

プロジェクトが未登録の場合は自動で `project.yml` が作成されます。コードを大きく変更したら再実行してください(差分がなければファイルは書き換わりません)。

### 2. MCP サーバーとしての利用

MCP サーバーは本家と同じ `serena start-mcp-server` です。この fork を指定して起動します。

**Claude Code:**

```bash
claude mcp add serena -- uvx --from git+https://github.com/TakumaAida/serena-code-map serena start-mcp-server --context ide-assistant --project $(pwd)
```

**Codex** (`~/.codex/config.toml`):

```toml
[mcp_servers.serena]
command = "uvx"
args = ["--from", "git+https://github.com/TakumaAida/serena-code-map", "serena", "start-mcp-server", "--context", "codex"]
```

その他のクライアントの設定方法は[本家 README](https://github.com/oraios/serena#readme) と同じです(`--from` の参照先をこの fork に変えるだけです)。

### 3. エージェントへの案内

生成された `.serena/code-map/AGENTS_SNIPPET.md` の内容を、プロジェクトの `AGENTS.md`(または `CLAUDE.md`)に追記してください。エージェントは次の運用になります:

1. セッション開始時に `overview.md` を読む
2. 必要なファイルの詳細だけ `modules/*.md` から読む
3. 正確な最新のシンボル確認・参照検索・編集には従来どおり Serena MCP を使う

## ライセンス

本家 Serena と同じく [MIT License](LICENSE) です。Copyright は Oraios AI に帰属します。
