# Japanese terminology (ja)

Shared terms for the Japanese catalogues (`apps/*/*/locale/ja.po`). Use them consistently;
keep ERPNext's own Japanese terms for standard documents where ERPNext already translates
them. Style: screen labels as concise noun phrases; messages as polite sentences
(です・ます); no trailing period on labels.

Keep as written (do not translate): placeholders `{0}` `{1}` `%s` `%(name)s`, HTML tags and
entities, product and company names (Hinode …), item and document codes, and the
abbreviations ASN, BOM, CAPA, ECO, ECR, EDI, KPI, MES, MRP, OEE, OTIF, VIN, WIP, API, HMAC
(a Japanese gloss in parentheses is fine on first use in a sentence).

Updating the catalogues (`i18n.py` runs in the bench container, `scripts/dev/i18n-host.sh`
runs the same commands on the host from the bootstrapped bench's sources):

1. `i18n.py pot` extracts the strings: Python, JavaScript, DocType and workspace JSON,
   templates and print formats.
2. `i18n.py po ja` merges them into each app's `ja.po`; `i18n.py fill ja` copies the
   translations another app's catalogue already has (Frappe keeps one per string).
3. Translate what remains with this glossary. `i18n.py check ja` must then report every
   string translated: CI runs it in the integration job, and `scripts/ci/lint.sh` checks
   completeness, placeholders and tags.

| English | 日本語 |
|---|---|
| Approval / Approve / Reject | 承認 / 承認する / 却下する |
| Approval Request / Approval Policy | 承認依頼 / 承認ポリシー |
| Approver / Requester | 承認者 / 依頼者 |
| Delegation / Delegate / Delegator | 権限委任 / 受任者 / 委任者 |
| Maker/checker | 作成者と承認者の分離 |
| Segregation of Duties (SoD) | 職務分掌 |
| Audit Event / audit trail | 監査イベント / 監査証跡 |
| hash chain / sealed | ハッシュチェーン / 封印済み |
| Cancel with reason | 理由を付けて取消 |
| Plant | 工場 |
| Production Line / Station | 生産ライン / ステーション |
| Shift / Shift Handover | シフト / シフト引継ぎ |
| Work Order / Job Card | 作業指示 / ジョブカード |
| BOM / BOM revision / effective date | 部品表 (BOM) / BOM改訂 / 有効開始日 |
| Engineering Change Request / Order | 設計変更依頼 (ECR) / 設計変更指示 (ECO) |
| Impact assessment / where-used | 影響評価 / 使用先 |
| Planning Run / Planning Parameter Set | 計画実行 / 計画パラメータセット |
| Proposal / Shortage / Capacity | 提案 / 欠品 / 能力 |
| Release (a plan, an order) | リリース |
| Plant Schedule / sequence / takt | 生産順序計画 / 順序 / タクト |
| Vehicle / Vehicle Unit / VIN | 車両 / 車両個体 / VIN（車台番号） |
| Vehicle Model / Variant / Configuration | 車種 / 仕様バリアント / 仕様構成 |
| Option / Trim / Drivetrain / Body Color | オプション / グレード / 駆動方式 / ボディカラー |
| Powertrain / Engine / Sub-assembly | パワートレイン / エンジン / サブアセンブリ |
| Kanban / Kanban Loop / Card / Signal | かんばん / かんばんループ / かんばんカード / かんばん信号 |
| Call-off | 納入指示 |
| Andon / Line Stop / Call | アンドン / ライン停止 / 呼出し |
| Acknowledge / Containment / Restart | 応答 / 封じ込め / 再稼働 |
| Quality Hold / Release Hold | 品質保留 / 保留解除 |
| Quality Inspection / Incoming / Final | 品質検査 / 受入検査 / 完成検査 |
| Nonconformance / Disposition / CAPA | 不適合 / 処置 / 是正・予防処置 (CAPA) |
| Quarantine / Scrap / Rework / Use As Is | 隔離 / 廃棄 / 手直し / 特別採用 |
| Return to Supplier | 仕入先返品 |
| Genealogy / Traceability | 構成履歴 / トレーサビリティ |
| Recall Campaign / Affected Vehicles | リコールキャンペーン / 対象車両 |
| Supplier / Supplier Portal | 仕入先 / サプライヤーポータル |
| Supplier Onboarding / Approved Supplier List | 仕入先登録 / 承認済み仕入先リスト |
| Sourcing Decision / Quotation / RFQ | 調達先決定 / 見積 / 見積依頼 (RFQ) |
| Purchase Order / Blanket Order | 発注 / 包括発注 |
| Delivery Schedule / firm / forecast | 納入計画 / 確定 / 内示 |
| Order Acknowledgement | 注文請書 |
| Advance Shipping Notice | 出荷事前通知 (ASN) |
| Purchase Receipt / Receipt Discrepancy | 入荷 / 入荷差異 |
| Three-way match / Match exception / Price variance | 3点照合 / 照合例外 / 価格差異 |
| Cycle Count / Consignment | 循環棚卸 / 預託在庫 |
| Transport Booking / Shipment Manifest | 輸送手配 / 出荷積荷目録 |
| Proof of Delivery / Carrier | 受領証明 / 運送会社 |
| Dealer / Dealer Portal / Dealer Order | 販売店 / ディーラーポータル / 販売店注文 |
| Allocation / Reservation | 引当 / 予約 |
| Delivery Acceptance / Warranty | 納車受領 / 保証 |
| Support Request | サポート依頼 |
| Intercompany / Transfer price | グループ会社間 / 移転価格 |
| Period Close / Waive / Consolidation | 締め処理 / 免除 / 連結 |
| Integration Partner / Inbound / Outbound message | 連携パートナー / 受信メッセージ / 送信メッセージ |
| Replay / Retry / Dead Letter | 再処理 / 再送 / デッドレター |
| Idempotency key | 冪等キー |
| Workspace / Dashboard / Report | ワークスペース / ダッシュボード / レポート |
| Raw material store (forward) / Reserve storage | 原材料倉庫 / ストック保管庫 (not 予約: that is reservation) |
| Put-away / Putaway Rule / Pick List | 格納 / 格納ルール / ピッキングリスト |
| Overview / Notices / Remittances | 概要 / お知らせ / 送金通知 |

## Standard documents and desk terms

Frappe and ERPNext ship no Japanese catalogue. automotive_core's catalogue also translates
the upstream strings our users meet most (scripts/dev/i18n.py, `UPSTREAM`). Frappe keeps one
translation per English string for the whole site, whichever app it comes from, so these
terms apply in every catalogue.

| English | 日本語 |
|---|---|
| Sales Order / Sales Invoice / Delivery Note | 受注 / 売上請求書 / 納品書 |
| Quotation / Customer / Customer Group | 見積 / 顧客 / 顧客グループ |
| Purchase Invoice / Supplier Quotation / Supplier Group | 仕入請求書 / 仕入先見積 / 仕入先グループ |
| Material Request / Payment Request | 資材要求 / 支払依頼 |
| Payment Entry / Journal Entry | 入出金伝票 / 仕訳伝票 |
| Stock Entry / Stock Reconciliation / Stock Ledger | 在庫移動 / 在庫調整 / 在庫元帳 |
| Stock Reservation Entry / Serial and Batch Bundle | 在庫予約 / シリアル・バッチバンドル |
| Item / Item Group / Item Price / Price List / Pricing Rule | 品目 / 品目グループ / 品目価格 / 価格表 / 価格ルール |
| UOM / Warehouse / Batch / Serial No | 単位 / 倉庫 / バッチ / シリアル番号 |
| Operation / Routing / Workstation / Production Plan | 工程 / 工程順序 / 作業区 / 生産計画 |
| Landed Cost Voucher | 付帯費用伝票 |
| Bank Transaction / Bank Reconciliation | 銀行取引 / 銀行照合 |
| Company / Account / Chart of Accounts / Cost Center | 会社 / 勘定科目 / 勘定科目表 / 原価センター |
| Fiscal Year / Accounting Period | 会計年度 / 会計期間 |
| General Ledger / Receivable / Payable | 総勘定元帳 / 売掛金 / 買掛金 |
| Grand Total / Net Total / Outstanding Amount | 総合計 / 正味合計 / 未決済額 |
| Tax Category / Tax Rule / Taxes and Charges Template | 税区分 / 税ルール / 税金・手数料テンプレート |
| Consumption tax | 消費税 |
| Posting Date / Due Date | 転記日付 / 期日 |
| Employee / Holiday List / Territory | 従業員 / 休日リスト / 地域 |
| Valuation Rate | 評価単価 |
| Save / Submit / Cancel / Amend | 保存 / 提出 / キャンセル / 修正 |
| Draft / Submitted / Cancelled | 下書き / 提出済み / 取消済み |
| Open / Closed / Pending / Completed / Failed | オープン / クローズ / 処理待ち / 完了 / 失敗 |
| Approved / Rejected / Active / Disabled | 承認済み / 却下済み / 有効 / 無効 |
| New / Edit / Delete / Duplicate / Rename | 新規 / 編集 / 削除 / 複製 / 名前を変更 |
| Print / Email / Attach / Attachment | 印刷 / メール / 添付 / 添付ファイル |
| Filter / Sort / Group By / Search | フィルター / 並べ替え / グループ化 / 検索 |
| Settings / Customize / Setup | 設定 / カスタマイズ / セットアップ |
| User / Role / Permission | ユーザー / ロール / 権限 |
| Status / Owner / Assigned To | ステータス / 作成者 / 担当者 |
| Comment / Timeline / Activity / Notification | コメント / タイムライン / アクティビティ / 通知 |
| Notes / Remarks / Description | メモ / 備考 / 説明 |
| Yes / No | はい / いいえ |

"Cancel" is both a dialog button and the document action; キャンセル serves both, and the
state stays 取消済み (as in 理由を付けて取消).
