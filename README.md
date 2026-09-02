# A 股日线形态筛选器

基于 Python 与 AkShare 的长期运行筛选器，无需 API Token。工作流在日终运行，通过交易日历选择最近三个开市日并将当天作为 `D2`；非交易日则回退到最近开市日。手动触发时，上海时间 16:00 前会使用前一日，以避免未收盘数据。若日历或任一日线快照不完整即停止，不会写入不完整结果。

## 形态规则

以连续三个交易日 `D0`、`D1`、`D2` 判断：

- `D1 close > D1 open`
- `D1 volume >= 1.5 × D0 volume`
- `D1 high > D1 close`
- `D2 high = D1 close`，且 `D2 low = D1 open`

价格匹配允许 A 股 `0.01` 元的一个最小报价单位偏差：例如 D1 收盘为 `10.50` 时，D2 最高价在 `10.49–10.51` 都符合。筛选范围排除名称包含 `ST`（含 `*ST`）的股票、科创板及北交所；主板和创业板保留。

## 本地运行

需要 Python 3.10+：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m a_share_screener.runner
```

每个 `D2` 交易日会生成两类中文表头 CSV，成交量均统一为“股”：

- `data/YYYYMMDD.csv`：当日全市场快照，包含每只非 ST、非科创板、非北交所股票的日期、开盘价、收盘价、最高价、最低价及成交量。
- `results/YYYYMMDD.csv`：以最近三个交易日快照筛选出的结果。各字段以 `D0(0831)`、`D1(0901)`、`D2(0902)` 形式标注实际交易日期。

最近三个交易日的 `data/YYYYMMDD.csv` 均已存在时，脚本会直接复用本地数据并重新筛选，不访问网络；因此调整筛选条件后可快速重跑。需要强制更新原始数据时使用 `python -m a_share_screener.runner --refresh-data`。数据通过 AkShare 的交易日历、腾讯 A 股现货列表与腾讯不复权历史日线接口获取，无需环境变量或密钥。

历史日线会默认使用 32 个并发请求；网络或数据源受限时可用 `--workers 8` 降低并发。

运行不访问网络的单元测试：

```bash
python -m unittest discover -s tests -v
```

## GitHub Actions

工作流位于 [`.github/workflows/screen.yml`](.github/workflows/screen.yml)，会在每个工作日北京时间 17:20 运行，也可在 **Actions** 页面手动触发。无需配置 GitHub Secret。它会先由脚本核验实际交易日，随后只提交 `results/` 下有变化的 CSV。

工作流声明了最小所需的 `contents: write` 权限，仅用于把新的结果 CSV 推送回仓库。若组织策略禁用了 Actions 写权限，请在仓库 **Settings → Actions → General** 中允许工作流读写权限。
