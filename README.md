# A 股日线形态筛选器

基于 Python 与 AkShare 的 A 股日线形态筛选器，无需 API Token。工作流每个交易日日终运行，使用最近三个开市日筛选，并每日更新 `data/` 行情快照和 `results/` 筛选结果。非交易日会使用最近开市日。

## 形态规则

以连续三个交易日 `D0`、`D1`、`D2` 判断：

- D1 收盘价高于 D1 开盘价。
- D1 成交量不少于 D0 成交量的 1.5 倍。
- D1 最高价高于 D1 收盘价。
- D2 最高价等于 D1 收盘价，且 D2 最低价等于 D1 开盘价。

价格匹配允许 A 股 `0.01` 元的一个最小报价单位偏差：例如 D1 收盘为 `10.50` 时，D2 最高价在 `10.49–10.51` 都符合。

## 股票池过滤规则

筛选器先从腾讯 A 股现货列表中仅保留 `stock_type` 以 `GP-A` 开头的证券，再排除以下股票：

- 股票名称（不区分大小写）包含 `ST`，因此 `ST` 与 `*ST` 均会排除。
- 股票代码以 `688` 或 `689` 开头的科创板股票。
- 股票代码以 `4`、`8` 或 `920` 开头的北交所股票。

沪深主板及创业板股票会保留。只有同时具备 D0、D1、D2 三个交易日日线数据的股票才进入形态判断。

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

脚本会复用最近三个交易日中已有的 `data/YYYYMMDD.csv`，只下载缺失日期的全市场快照，再重新筛选；因此日常运行通常只下载新的 D2 数据，调整筛选条件后也可快速重跑。需要强制更新这三个交易日的原始数据时使用 `python -m a_share_screener.runner --refresh-data`。数据通过 AkShare 的交易日历、腾讯 A 股现货列表与腾讯不复权历史日线接口获取，无需环境变量或密钥。

历史日线默认使用 8 个并发请求，每个请求超时为 30 秒。每只股票完成后会先写入 `data/.YYYYMMDD.partial.csv` 检查点；中断后的下一次运行会跳过已在三个检查点完成的股票。任务完成时才原子替换为正式 CSV，成功后删除检查点。

运行不访问网络的单元测试：

```bash
python -m unittest discover -s tests -v
```

## GitHub Actions

工作流位于 [`.github/workflows/screen.yml`](.github/workflows/screen.yml)，会在每个工作日北京时间 17:20 运行，也可在 **Actions** 页面手动触发。无需配置 GitHub Secret。它会先由脚本核验实际交易日，随后只提交 `results/` 下有变化的 CSV。

工作流声明了最小所需的 `contents: write` 权限，仅用于把新的结果 CSV 推送回仓库。若组织策略禁用了 Actions 写权限，请在仓库 **Settings → Actions → General** 中允许工作流读写权限。
