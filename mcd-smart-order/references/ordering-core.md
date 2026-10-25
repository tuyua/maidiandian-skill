# 麦当劳 MCP 共享调用规范

所有模式共用本文档。调用任何下单链路工具前先读这里，避免参数组合错误。

## 〇、交易安全前置链（create-order 前强制门禁）

本规范绑定 `references/txn-security-prompts.md`（交易安全闭环契约）。凡涉及 `create-order` / `cancel-order` 的动作，必须先满足以下强制门禁。**这些由智能体在运行时执行，不由离线脚本承担**——脚本只做组合枚举与预估（见 SKILL.md 本地脚本清单），不调用任何写接口，因此门禁必须在 Agent 侧落地。

### 门禁清单（create-order 前必须逐项满足）

1. **【P2 确认卡】** 向用户完整展示「下单前确认卡」，含六要素 + 不可逆提示：
   - 门店：storeCode + 门店名 + 城市·地标（得来速/外送含 beCode）
   - 履约方式：到店自取 / 得来速 / 麦乐送 / 企业团餐（含 takeWayCode 或 addressId）
   - 商品清单：逐项 名称 × 数量 × 单价(元) × 小计(元)
   - 券：已应用券名与抵扣；未应用券明确原因
   - 费用：商品小计 / 优惠 / 运费(外送) / 随单购会员卡（若含，单列并征得同意）
   - 实付金额：仅 `calculate-price` 返回的 price÷100 元可称「实付」
   - 不可逆提示：「订单创建后不可撤销价格与内容，支付前可取消；点『确认下单』即视为授权创建订单」
2. **【P3 幂等】** 每次 `create-order` 必须生成并携带幂等键 `idemKey`（见下方生成规则），重试复用同一键；调前先以该键核查是否已存在同键订单，避免重复下单。
3. **【P6 二次算价】** 在用户点击确认下单同一轮、`create-order` 之前，再次调用 `calculate-price` 校验；二次实付与确认卡实付必须完全一致，不一致立即拦截并重走确认卡。
4. **【R1 风险分级】** 读=L0、取消=M1、创建/复购=H1；H1 确认覆盖率必须 100%。
5. **【P5 审计】** 写操作（create/cancel/reorder）必须落审计（见下方最小审计字段），手机号脱敏、地址仅记城市·地标、禁止记录支付令牌/Cookie/完整卡号。
6. **【R2 护栏】** 遇「跳过确认直接下单 / 全量自动下单 / 跨平台支付编排 / 未验证门店履约 / 静默降硬约束」类请求，立即拒绝并记为 blocked，不执行。

### 幂等键生成规则（P3）

idemKey = HMAC( userStableId + storeCode + beType + orderType + 规范化items(JSON) + takeWayCode/addressId + 金额分 )。
- 同一意图（同门店/同商品/同金额/同履约）复用同一键；
- 超时或不确定返回时，用同一 idemKey 调 `query-order` 核查，绝不以新键重发。

### 最小审计字段（P5）

每条写操作记录：ts、sessionId、action(create/cancel/reorder)、riskLevel(L0/M1/H1)、idemKey、storeCode、orderId(成功后回填)、items 摘要、实付(分)、结果(success/fail/blocked)、失败原因。

### 异常分支（P4）

- 超时/不确定：先 `query-order` 核查，禁止盲目重发。
- 失败重试：仅可重试错误才复用同键重试，次数 ≤3 且指数退避。
- 取消：`cancel-order` 必填 `cancelReasonCode`，取消后再 `query-order` 确认。
- 转人工：连续失败≥3次 / 金额与确认卡偏差超阈值 / 幂等冲突无法自决 / 用户明确要求 时，停止自动处理并输出「已转人工」+ 脱敏上下文快照。


### 订单异常自助处理（P4 展开，功能机会建议 3.2）

凡出现「创建超时 / 状态不明 / 取消失败 / 嫌疑重复提交」，按下述流程处理，**任何分支都不得直接重新 create-order**：

```text
异常信号
 → query-order 查询（用订单号或幂等键关联的订单）
 → 状态判定
     ├─ 已创建·待支付 → 展示订单详情，询问是否继续支付 / 取消
     ├─ 已取消        → 明确告知已取消，询问是否重新选购（重新选购须重走确认卡）
     ├─ 未创建        → 说明未查询到订单，重走确认卡并重新获得授权后再创建
     └─ 状态未知/接口异常 → 不猜测、不重试；提供「继续查询 / 取消 / 转人工」三选
```

1. **先查后做**：异常第一动作永远是 `query-order`，不是重发 `create-order`，也不是凭用户口头描述下结论。
2. **状态未明禁止重试**：查询结果无法判定时，禁止用新幂等键重发；复用原 `idemKey` 查询，仍不明则转人工。
3. **取消前三确认**：向用户确认目标订单（门店 + 商品摘要 + 实付金额），得到单独肯定答复后才调 `cancel-order`；取消后必须再 `query-order` 核实，以查询结果为准。
4. **审计留痕**：异常处理全过程记录 action、idemKey、orderId、查询次数、最终状态与转人工原因。
5. **重复下单零容忍**：同一 `idemKey` 已对应订单时，禁止再次创建；如用户确实要下第二单，视为新意图，重新走完整确认卡。
## 一、场景与参数的对应关系（最容易出错的地方）

`beType` 决定一切。选错场景，后续所有工具都会报错。

| 场景 | beType | orderType | beCode | 门店来源 |
|---|---|---|---|---|
| 到店自取 | 1 | 1 | **不传**（传了会报错） | `query-nearby-stores` |
| 得来速（车道取餐） | 5 | 1 | **必传** | `query-nearby-stores` |
| 麦乐送到家 | 2 | 2 | **必传** | `delivery-query-stores` |
| 企业团餐 | 6 | 2 | **必传** | `delivery-query-stores` |

规则记忆法：**到店自取是唯一不传 beCode 的场景**；只要是外送（orderType=2）或得来速，就必须携带 beCode，且 beCode 必须与 storeCode 配对使用。

`reservationDate` 仅预约场景必传，格式 `yyyy-MM-dd HH:mm`；非预约场景**不要传**。

## 二、标准调用链

```
query-nearby-stores / delivery-query-stores   → 拿 storeCode（+ beCode）
                ↓
query-meals(storeCode, orderType, beType)     → 拿菜单与 productCode
                ↓
available-coupons / query-my-coupons          → 拿可用券（可选）
                ↓
calculate-price(items...)                     → 拿真实应付金额 + takeWayList
                ↓
create-order(...)                             → 仅用户明确确认后调用
```

**任何一步都不能跳过。** 尤其禁止跳过 `query-meals` 直接凭菜名构造 `productCode`。

## 三、关键返回字段说明

### query-meals

返回结构是「分类索引 + 详情映射表」两段式：

- `data.categories[]`：分类导航，每项含 `name` 和 `meals[]`（只有 `code` 和 `tags`）。
- `data.meals`：以 `code` 为键的详情映射，含 `name`、`currentPrice`（现价，**单位元**）、`originalPrice`（原价/划线价）、`discountType`、`canWithOrder`、`withOrder`。

读价格要查 `data.meals[code]`，不要在 `categories` 里找价格——那里没有。

`discountType` 取值：`null`-不享受优惠、"早餐卡优惠"、"促销优惠"、"麦金卡优惠"、"随单购早餐卡优惠"、"随单购麦金卡优惠"。

`canWithOrder=true` 且带 `withOrder` 字段时，说明该商品必须**随单购**才能享受优惠价，否则按原价结算。随单购参数：

```json
"withOrder": {
  "membershipCode": "106",
  "membershipSpecId": "880"
}
```

注意：`query-meals` 返回的 `withOrder` 里字段名是 `specId`，传给 `calculate-price` 时要改名为 `membershipSpecId`。

> **已验证（2026-10-25 真实只读实测，门店 1420450 / 到店 beType=1）**：
> - 取 `9900016309`（龙焰鸡腿堡四件套，`canWithOrder=true`，菜单 `currentPrice=29` / `originalPrice=57`，`withOrder={membershipCode:"106",specId:"880"}`）。
> - 不传 `withOrder` 调用 `calculate-price` → 该餐品按**原价 57 元**结算（`discount=0`），印证「不选随单购则按原价购买」。
> - 传 `withOrder={membershipCode:"106",membershipSpecId:"880"}`（已改名）→ 餐品降至 **29 元**，并自动追加 `521234 麦金卡体验卡（15天）` **9.9 元**，实付 **38.9 元**（`discount=2800` 分）。随单购优惠确为「额外购买会员卡换来的餐品折扣」，必须计入总价并征得同意（见 SKILL.md 禁止事项）。
> - 容错发现：实测中即便误用 `specId`（未改名）传入，`calculate-price` 仍正确应用随单购优惠（结果与改名一致）。即 `membershipCode` 是关键字段，API 对 `specId`/`membershipSpecId` 命名在本场景下均容错。**遵循文档改名仍是稳妥做法**，避免个别门店/规格要求精确字段名时报错。

### calculate-price

- **所有金额字段单位是分**，`price` 是实付，`originalPrice` 是原价，`discount` 是优惠额。展示时除以 100。
- `data.takeWayList[]`：取餐方式列表，`code` 是到店下单（`orderType=1`）时 `create-order` 的**必填参数**。实测常见值为 `eat-in`（堂食）、`take-in-store`（外带）。必须让用户选，不要默认。
- `data.enjoyed`：已享受的优惠；`data.enjoyable`：还差一点就能享受的优惠，`balance` 是门槛差额（单位分）。**这是凑单的关键信号**——告诉用户「再加 X 元就能减 Y 元」。
- `data.productList[]`：逐项小计，用于展示明细。

### create-order

- `takeWayCode`：`orderType=1`（到店/得来速）必传，取自 `calculate-price` 的 `takeWayList[].code`；外送场景不传。
- `addressId`：`orderType=2`（麦乐送/团餐）必传，来自 `delivery-query-addresses`。
- `gmServiceCode`：企业团餐场景必传，来自 `query-meal-assistance`。
- `remark`：仅外送场景需要引导用户填写，最多 50 字。
- 下单成功后引导用户扫码或打开麦当劳 App 支付，并提示支付完成后回复「支付完成」以便查询订单状态。

## 四、套餐子项与特调（遇到再展开）

需要修改套餐组成或特调口味时，调 `query-meal-detail` 拿 `modification`。传参规则极容易出错：

- 用户**选中**的特调项：`key` = 返回的 `selectedKey`。
- 用户**未选中**的特调项：若该项 `unselectedKey` 不为空，也必须传入，`key` = `unselectedKey`。
- 即：只要某个特调组含 `unselectedKey`，该组内所有项都必须传，不能只传选中的。

不要主动展开特调选项列表，用户不问就不展示。

## 五、营养接口返回的是格式化字符串

`list-nutrition-foods` 的 `data` 不是 JSON 数组，而是一段格式化文本，形如：

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
  圆筒冰淇淋,null,391,93,2,3,14,36,77
```

按行解析，字段顺序固定：`产品名, 能量描述, 千焦, 千卡, 蛋白质, 脂肪, 碳水, 钠, 钙`。

- 只有**常见餐品**有数据，新品（如龙焰鸡腿堡、灰焰圆筒）经常查不到；查不到一律按「未核实」处理，不得填 0 或估算。
- 营养条目名与菜单名不完全一致（营养库是「中薯条」，菜单可能只写「薯条」），匹配时需容错比对，匹配不上就标未核实。

## 六、已验证的调用示例（深圳福田高铁站餐厅，到店自取）

```json
// 1. 查门店
{"beType": 1, "searchType": 2, "city": "深圳", "keyword": "福田"}
// → storeCode: "1420450"

// 2. 查菜单
{"storeCode": "1420450", "orderType": 1, "beType": 1}
// → data.meals["9900014838"] = {name:"枫糖风味厚松饼猪柳蛋堡三件套", currentPrice:"27.9", originalPrice:"39.5"}

// 4. 下单（到店场景必须带 takeWayCode）
{"storeCode":"1420450","orderType":1,"beType":1,"takeWayCode":"take-in-store",
 "items":[{"productCode":"515837","quantity":1}],"needTableware":false}
// → orderId: "1030717660000755255726423780"，orderStatus: 待支付，实付 5 元

// 5. 取消（cancelReasonCode: 1=改主意了 2=重复下单 3=点错/点多 4=地址电话错 5=时间选错 -1=其它）
{"orderId":"1030717660000755255726423780","cancelReasonCode":"1"}
// → cancelResult: true，再次 query-order 得到 orderStatus: 订单已取消
```

实测补充：

- 到店自取传 `beType=1` 时不传 `beCode`，下单成功，证明该组合正确。
- `takeWayList` 实测值：`eat-in`（堂食）、`take-in-store`（外带）。
- 未支付订单 `pickupCode` 为空，支付后才会有取餐码。
- 订单状态以 `query-order` 返回的 `orderStatus` 文案为准（实测 `10`=待支付、`60`=已取消），接口文档的枚举说明与真实值不一致。

## 七、常见错误速查

| 报错 / 现象 | 原因 | 处理 |
|---|---|---|
| 收藏餐厅列表为空 | `query-nearby-stores` 用了 `searchType=1` 但用户无收藏 | 按三级定位策略：改 `searchType=2`（城市+地标），关键词可从 `delivery-query-addresses` 提取；都没有才问用户 |
| 想按「附近」定位 | 接口**没有经纬度参数**，无法真 GPS 定位 | 用收藏/地址簿兜底；`distance` 是相对搜索关键词的距离，不得声称已定位到用户位置 |
| 到店场景报参数错误 | 多传了 `beCode` | 到店自取（beType=1）不传 beCode |
| 外送场景报参数错误 | 用了到店接口的门店 | 外送必须用 `delivery-query-stores` 重新取门店 |
| 金额差 100 倍 | 把分当元 | `calculate-price` 结果除以 100 |
| 下单报 takeWay 缺失 | 到店场景没传 `takeWayCode` | 从 `calculate-price` 的 `takeWayList` 取值 |
| 说有优惠但算价没减 | 券不适用于当前门店/渠道 | 以 `calculate-price` 实际返回为准 |
| 推荐里出现丝巾/毛绒周边 | 菜单里混有**非食品周边**，与餐品同菜单且带价格，还会被嵌进「人气热卖」「小食甜品/其他」等食品分类 | **脚本已做确定性兜底**：`mcd_common.resolve_category` 按已知编码与名称关键词（丝巾/周边/毛绒/玩具/公仔/礼品卡…）强制归为「周边」并丢弃，返回里的 `non_food_excluded` 是剔除计数。仍需传 `exclude_categories: ["周边"]` 作为双保险；任何模式都不得推荐非食品 |

## 八、优惠方案与异常订单补充规则

### 8.1 优惠方案比较

候选方案至少区分：无券、已有卡包券、满足券门槛的凑单、套餐或随单购。每个候选都必须经过实时 `calculate-price` ，按实付金额排序。输出应包含商品小计、优惠、额外加购、配送费/打包费、实付及相对无券方案的差额。

凑单解释必须同时说明：为使用优惠多花多少钱、实际节省多少钱、是否值得以及新增商品是否符合用户需求。不得为了使用优惠券强行加购。门店、渠道、商品、数量、券或费用变化后必须重新算价。

**只能量力界限**：可领取的券不返回 `couponId` ，无法带入 `calculate-price` 试算；只有卡包（`query-my-coupons`）中的券才能尝试带券计算。券不可用时，展示接口返回的真实原因，不猜测原因。

**最优惠的定义**：只有 `calculate-price` 返回的实付金额可用于宣称「更便宜」「最划算」。菜单预估价、活动宣传价、券面额均不得直接当作实付或据此宣称节省金额。

### 8.2 异常订单处理

- 创建超时或状态未知：先调用 `query-order` ，不得直接重试创建。
- 查询已创建：核对订单详情，不得重复创建。
- 查询未创建：重新展示完整方案并重新取得创建授权，复用原意图的幂等规则。
- 取消前：确认订单号、门店、履约方式和金额；调用取消后再次查询确认。
- 状态未被接口明确确认时，不得宣称创建、取消或支付成功。
- 状态仍无法判定时，提供「继续查询 / 取消订单 / 转人工」三个选项，由用户决定下一步，不自作主张重试。
