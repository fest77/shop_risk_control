/* 规则编辑抽屉 `editCard`（模块 06-B Spec §2.2.3 / §2.3 / BR-06-01~09）
 *
 * ## 这个文件负责的三件"容易做错"的事
 *
 * 1. **乐观锁**（BR-06-05）：编辑必须带 `expected_version`；服务端返回
 *    `CFG-4006` 时要给出「该规则已被他人修改（当前 vN），请刷新后重试」这种
 *    人话，而不是把原始 JSON 甩到界面上，也不能静默覆盖别人的改动。
 * 2. **错误定位**（Spec §2.3）：`CFG-4003` 的 `errors[{path, message}]` 交给
 *    `cond_tree.js` 按 `path` 高亮到具体节点行 + 抽屉顶部汇总「条件树有 N 处问题」。
 * 3. **失败不丢用户输入**（BR-06-18）：任何校验/保存失败都**不清空、不关闭**抽屉。
 *
 * 规则编码由服务端生成（BR-06-01），前端**不提供**编码输入框；编码只读展示（BR-06-02）。
 */
import * as api from '../api.js';
import * as ui from '../ui.js';
import { optionsOf, labelOf } from '../store.js';
import { createCondTree, ensureFeatureMeta, featureMetaReady, featureMetaError } from './cond_tree.js';

const RULE_URL = `${api.API_PREFIX}/rules`;
const IMPACT_URL = (code) => `${RULE_URL}/${encodeURIComponent(code)}/impact`;

/** 失败码 → 人话（Spec §5.1 的处置策略各不相同，不能只甩 message）。 */
function saveErrorText(e) {
  switch (e && e.code) {
    case 'CFG-4003': return '条件树有结构问题，已按位置标注（详见下方）。';
    case 'CFG-4004': return '命中分值必须在 0~100 之间（服务端拒绝）。';
    case 'CFG-4006': {
      const cur = e.data && e.data.current_version;
      return `该规则已被他人修改（当前 v${cur === undefined ? '?' : cur}），请刷新后重试。`;
    }
    case 'CFG-4013': return e.message || '归属场景不存在，请选择规则场景字典里的场景。';
    case 'CFG-4007': return '规则编码不可修改（编码由服务端生成）。';
    case 'CFG-4002': return '规则编码已占用，请重试保存。';
    case 'CFG-4001': return '规则不存在或已被删除。';
    case 'CFG-5003': return '操作未完成（审计写入失败），已回滚，请重试。';
    case 'CFG-5001': return '规则保存失败，请稍后重试（线上规则集未变）。';
    default: return `${e && e.message ? e.message : '保存失败'}`;
  }
}

function field(label, node, required = false, span = 1, hint = '') {
  return ui.h('div', { class: 'field', style: span > 1 ? `grid-column:span ${span}` : null }, [
    ui.h('label', {}, [
      document.createTextNode(label),
      required ? ui.h('span', { class: 'req', text: ' *' }) : null,
    ]),
    node,
    hint ? ui.h('div', { class: 'hint', text: hint }) : null,
  ]);
}

function uuid() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID();
  return `k-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

/**
 * 打开规则编辑抽屉。
 * @param {object|null} rule 编辑时传规则对象，新建传 null
 * @param {{onSaved?:Function}} opts onSaved(saved) —— 保存成功后由列表视图刷新
 */
export async function openRuleEditor(rule, { onSaved, onStale, checkDuplicate } = {}) {
  const isNew = !rule;
  const code = isNew ? '' : String(rule._id || rule.rule_code || '');
  let expectedVersion = isNew ? null : Number(rule.version || 1);
  let dirty = false;
  const idemKey = uuid();   // 同一次保存重试复用（Spec §3.1 幂等键）

  await ensureFeatureMeta();

  const markDirty = () => { dirty = true; };

  // ---------- 表单控件 ----------
  const nameI = ui.inputBox({ value: isNew ? '' : (rule.name || ''), placeholder: '1~50 字，同场景内不重名', maxlength: 50 });
  const scenes = optionsOf('rule_scenes');
  const sceneS = ui.selectBox(
    scenes.length ? scenes : [{ value: '', label: '（规则场景字典为空）' }],
    isNew ? (scenes[0] ? scenes[0].value : '') : (rule.scene_code || ''),
  );
  const scoreI = ui.h('input', { type: 'number', min: '0', max: '100', step: '1',
    value: isNew ? '' : String(rule.score === undefined ? '' : rule.score) });
  const prioI = ui.h('input', { type: 'number', step: '1',
    value: String(isNew ? 10 : (rule.priority === undefined ? 10 : rule.priority)) });
  const statS = ui.selectBox(optionsOf('rule_status'), isNew ? 'disabled' : (rule.status || 'disabled'));
  const descI = ui.h('textarea', { rows: '2', maxlength: '200', placeholder: '≤200 字（选填）' });
  if (!isNew && rule.description) descI.value = rule.description;

  [nameI, scoreI, prioI, descI].forEach((el) => {
    el.addEventListener('input', markDirty);
    el.addEventListener('change', markDirty);
  });
  [sceneS, statS].forEach((el) => el.addEventListener('change', markDirty));

  // ---------- 条件树 + 只读 JSON 预览 ----------
  // JSON 预览必须**跟着编辑实时更新**（Spec §2.3「抽屉底部可展开当前树的 JSON」）：
  // 若只在展开那一刻渲染一次，用户改完条件再展开看到的是旧树，比不提供更误导。
  const jsonPre = ui.h('pre', { class: 'mono cond-json-body' });
  const jsonBox = ui.h('details', { class: 'cond-json' }, [
    ui.h('summary', { text: '查看 JSON（只读，便于与模块 05 排查分歧）' }),
    jsonPre,
  ]);
  const refreshJson = () => { jsonPre.textContent = JSON.stringify(tree.getTree(), null, 2); };

  const tree = createCondTree({ onChange: () => { markDirty(); refreshJson(); } });
  if (!isNew) tree.setTree(rule.condition);

  // 特征清单是 `field` 下拉的唯一来源（BR-06-16）；拉不到就说清楚，而不是给一个空下拉
  const featureNote = ui.h('div', { class: 'hint' });
  const renderFeatureNote = () => {
    if (featureMetaReady()) {
      ui.mount(featureNote, ui.h('span', { text:
        `特征字段来自 GET /api/v1/features/meta（04 特征引擎公布的 ${featureMetaReady().length} 项白名单），不支持自由输入字段名。` }));
    } else {
      ui.mount(featureNote, [
        ui.h('span', { class: 'cond-warn', text: `特征清单加载失败：${featureMetaError() || '未知原因'}。字段下拉将不完整，请勿此时保存。` }),
        (() => { const b = ui.button('重试加载', { variant: 'ghost', onClick: async () => {
          await ensureFeatureMeta(); tree.refreshFeatures(); renderFeatureNote();
        } }); b.classList.add('btn-sm'); return b; })(),
      ]);
    }
  };
  renderFeatureNote();

  // ---------- 错误区 ----------
  const errBox = ui.h('div', { class: 'form-error' });
  const showErr = (msg) => { errBox.textContent = msg; errBox.classList.add('on'); };
  const clearErr = () => { errBox.textContent = ''; errBox.classList.remove('on'); tree.clearErrors(); };

  // ---------- 关闭（有改动要二次确认，Spec §2.2.3）----------
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    document.removeEventListener('keydown', onKey);
    if (root.parentNode) root.parentNode.removeChild(root);
  };
  const requestClose = () => {
    if (!dirty) { close(); return; }
    ui.modal({
      title: '放弃未保存的修改？',
      body: [ui.h('div', { class: 'hint', text: '抽屉里还有未保存的改动，关闭后这些内容会丢失。' })],
      confirmText: '放弃修改',
      variant: 'danger',
      onConfirm: () => { close(); return true; },
    });
  };
  const onKey = (e) => { if (e.key === 'Escape') requestClose(); };

  // ---------- 保存 ----------
  async function doSave({ thenSim = false } = {}) {
    clearErr();
    const name = nameI.value.trim();
    const scene = sceneS.value;
    const scoreRaw = scoreI.value.trim();
    const prioRaw = prioI.value.trim();

    // 这里只做"零成本"的必填/越界提前提示；**结构合法性一律以服务端为准**（BR-06-17）。
    if (!name) { showErr('请填写规则名称（1~50 字）。'); return; }
    if (!scene) { showErr('请选择归属场景（取值来自规则场景字典，BR-06-07）。'); return; }
    if (scoreRaw === '') { showErr('请填写命中分值（0~100 的整数）。'); return; }
    const score = Number(scoreRaw);
    if (!Number.isInteger(score)) { showErr('命中分值必须是 0~100 的整数。'); return; }
    if (score < 0 || score > 100) { showErr('命中分值必须在 0~100 之间（服务端仍会复核：CFG-4004）。'); return; }

    const local = tree.localIssues();
    if (local.length) {
      tree.highlight(local);
      showErr(`条件树有 ${local.length} 处未填完，请先补全（本地只做“未填完”短路，结构合法性由服务端判定）。`);
      return;
    }

    const rawTree = tree.getTree();
    ui.buttonBusy(btnSave, true, '保存中');
    ui.buttonBusy(btnSim, true, '处理中');
    try {
      // 同场景内重名：**由前端校验**（Spec §2.2.3 把它定义为 UI 校验；服务端刻意不拦，
      // 见决策"场景内规则重名只做前端校验"）。它必须走服务端搜索而不是"只看当前页"——
      // 规则一多就会跨页，只看本页等于把重名放过去。查不动时**不阻断保存**，
      // 但也不静默：明确 toast 说明这次跳过了重名检查。
      if (checkDuplicate) {
        try {
          if (await checkDuplicate(name, scene, code)) {
            showErr(`同场景下已存在名为「${name}」的规则（§2.2.3：同场景内不重名）。请换一个名称后再保存。`);
            return;
          }
        } catch (e) {
          ui.toast(`同场景重名校验未能完成（${e.message}），本次已跳过该提示`, 'warn', 5000);
        }
      }
      // BR-06-17：保存前**必须**调用 05 的 validate_tree()（经本模块的转发接口）
      const v = await tree.validate(rawTree);
      if (v.unavailable) {
        // 校验接口不可用 ≠ 条件树非法：如实告知，并继续交给保存接口复核
        // （保存接口同样走 validate_tree，权威性不降低；这里绝不静默跳过）
        showErr(`条件树校验接口调用失败（${v.unavailable.code || 'NET'} ${v.unavailable.message}），`
          + '已改由保存接口在服务端复核同一棵树。');
      }
      if (v.valid === false) {
        tree.highlight(v.errors);
        showErr(`条件树有 ${v.errors.length} 处问题，已在下方按位置标注（服务端 validate_tree 的结论）。`);
        return;
      }

      const payload = {
        name,
        scene_code: scene,
        description: descI.value.trim(),
        condition: v.normalized || rawTree,
        score,
        priority: prioRaw === '' ? 10 : Number(prioRaw),
        status: statS.value,
      };

      let saved;
      if (isNew) {
        saved = await api.request(RULE_URL, {
          method: 'POST', body: payload, silent: true,
          headers: { 'Idempotency-Key': idemKey },
        });
      } else {
        saved = await api.request(`${RULE_URL}/${encodeURIComponent(code)}`, {
          method: 'PUT', body: Object.assign({ expected_version: expectedVersion }, payload), silent: true,
        });
        if (saved && saved.version !== undefined) expectedVersion = Number(saved.version);
      }

      dirty = false;
      const vv = saved && saved.version !== undefined ? `v${saved.version}` : '';
      ui.toast(isNew
        ? `规则已创建：${(saved && (saved._id || saved.rule_code)) || ''} ${vv}（默认停用，仿真通过后再启用）`
        : `规则已保存：${code} ${vv}`, 'ok', 5000);
      if (onSaved) onSaved(saved);
      close();
      if (thenSim) {
        // BR-06-09：启用前建议先在仿真页验证。模块 10 尚未接入时，目标页会如实显示"尚未接入"。
        ui.toast('已送仿真验证：请到「事件仿真」页用该规则跑一次，通过后再启用（BR-06-09）。', 'warn', 6000);
        location.hash = '#/sim';
      }
    } catch (e) {
      // BR-06-18：失败**不关闭抽屉、不清空输入**
      if (e && e.code === 'CFG-4003') {
        const errs = (e.data && Array.isArray(e.data.errors)) ? e.data.errors : [{ path: '', message: e.message }];
        tree.highlight(errs);
        showErr(`条件树有 ${errs.length} 处问题，已按位置标注（服务端 validate_tree 的结论）。`);
      } else {
        showErr(saveErrorText(e));
      }
      if (e && e.code === 'CFG-4006') {
        // 冲突时给一个显式的"载入服务端最新版本"动作：由用户决定是否丢弃本地编辑。
        // 同时刷新列表——抽屉外那份行数据的 version 已经过期，不刷新的话下次点
        // 「启用」会拿旧版本再撞一次 409（提示正确但用户白挨一次）。
        if (onStale) onStale();
        const btn = ui.button('载入服务端最新版本', { variant: 'ghost', onClick: () => reloadLatest() });
        btn.classList.add('btn-sm');
        errBox.appendChild(btn);
      }
    } finally {
      ui.buttonBusy(btnSave, false);
      ui.buttonBusy(btnSim, false);
      refreshJsonIfOpen();
    }
  }

  function refreshJsonIfOpen() { if (jsonBox.open) refreshJson(); }

  /** 乐观锁冲突后拉取服务端最新版本（显式用户动作，因此允许覆盖本地未保存内容）。 */
  async function reloadLatest() {
    try {
      const fresh = await api.get(`${RULE_URL}/${encodeURIComponent(code)}`, null, null);
      const r = (fresh && fresh.item) ? fresh.item : fresh;
      if (r && r.version !== undefined) expectedVersion = Number(r.version);
      if (r) {
        nameI.value = r.name || '';
        if (r.scene_code) sceneS.value = r.scene_code;
        scoreI.value = r.score === undefined ? '' : String(r.score);
        prioI.value = String(r.priority === undefined ? 10 : r.priority);
        statS.value = r.status || 'disabled';
        descI.value = r.description || '';
        tree.setTree(r.condition);
      }
      dirty = false;
      clearErr();
      // 标题必须跟着刷新：否则"已载入 v2"之后标题还写着 v1，用户不知道该以哪个为准
      titleEl.textContent = `${code} · v${expectedVersion}`;
      if (onStale) onStale();   // 列表里的行版本同步刷新，避免下一次操作又拿旧版本
      ui.toast(`已载入服务端最新版本（v${expectedVersion}）`, 'ok');
    } catch (e) {
      showErr(`载入最新版本失败：${e.message}`);
    }
  }

  const btnSave = ui.button(isNew ? '保存（version+1，默认停用）' : '保存（version+1）',
    { variant: 'primary', onClick: () => doSave() });
  const btnSim = ui.button('送仿真验证', { variant: 'ghost', onClick: () => doSave({ thenSim: true }) });
  const btnCancel = ui.button('取消', { variant: 'ghost', onClick: requestClose });

  // ---------- 组装 ----------
  const title = isNew ? '新建规则' : `${code} · v${expectedVersion}`;
  const titleEl = ui.h('div', { class: 'drawer-title', text: title });
  const drawer = ui.h('div', { class: 'entity-drawer rule-drawer' }, [
    ui.h('div', { class: 'drawer-head' }, [
      titleEl,
      ui.h('div', { class: 'hint', text: isNew
        ? '规则编码由服务端生成（R{场景码}{3位序号}，BR-06-01），前端不填编码。'
        : '编码只读：创建后不可修改（BR-06-02）；保存必须携带 expected_version（BR-06-05）。' }),
    ]),
    ui.h('div', { class: 'form-grid' }, [
      field('规则名称', nameI, true),
      field('归属场景', sceneS, true, 1,
        scenes.length ? '' : '规则场景字典为空：请先执行 scripts/seed.py 灌入 E06 场景数据。'),
      field('命中分值（0~100）', scoreI, true),
      field('执行优先级（小者在前）', prioI),
      field('状态', statS, false, 1, '新建默认停用；启用前建议先仿真验证（BR-06-09）。'),
      field('规则说明', descI, false, 2),
    ]),
    ui.card('触发条件（条件树）', [
      ui.h('div', { class: 'cond-hint' }, [
        ui.h('span', { class: 'hint', text:
          '结构：组节点 {logic:"and"|"or", children:[…]}，叶子 {field, op, value}；exists 不接受 value（BR-06-15）。' }),
      ]),
      featureNote,
      tree.el,
      tree.summary,
      jsonBox,
    ], { soft: true }),
    errBox,
    ui.h('div', { class: 'drawer-actions' }, [btnSave, btnSim, btnCancel]),
  ]);

  const root = ui.h('div', { class: 'drawer-root' }, [
    ui.h('div', { class: 'drawer-mask', onclick: requestClose }),
    drawer,
  ]);
  document.body.appendChild(root);
  // 下一帧再加 open：同一个样式批次里改动不会产生过渡动画
  requestAnimationFrame(() => { if (root.parentNode) drawer.classList.add('open'); });
  document.addEventListener('keydown', onKey);
  // 打开时就渲染一次 JSON 预览：`<details>` 未展开时里面是空的，等到"点开才填"
  // 会让自动化与人工都以为预览坏了（此处一次 stringify 的成本可以忽略）
  refreshJson();
  setTimeout(() => { try { nameI.focus(); } catch (e) { /* 焦点失败不影响可用 */ } }, 0);

  return { close, root };
}

/** 规则删除的二次确认（Spec §5.2：编码 + 名称 + 近 30 天命中次数 + 快照语义提示）。 */
export function openDeleteConfirm(rule, onDone) {
  const code = String(rule._id || rule.rule_code || '');
  const impact = ui.h('span', { class: 'v', text: '查询中…' });
  const errBox = ui.h('div', { class: 'form-error' });

  const kv = (k, v) => ui.h('div', { class: 'kv' }, [
    ui.h('span', { class: 'k', text: k }),
    v instanceof Node ? v : ui.h('span', { class: 'v', text: String(v) }),
  ]);

  ui.modal({
    title: '删除规则',
    confirmText: '确认删除',
    variant: 'danger',
    body: [
      ui.h('div', { class: 'kv-list' }, [
        kv('规则编码', ui.h('span', { class: 'mono', text: code })),
        kv('规则名称', rule.name || '—'),
        kv('当前版本', `v${rule.version === undefined ? '—' : rule.version}`),
        kv('近 30 天命中次数', impact),
      ]),
      ui.h('div', { class: 'impact-note', text:
        '删除为软删除（BR-06-10）：历史决策记录不受影响（命中明细为快照），但该规则不再参与后续决策。' }),
      errBox,
    ],
    onConfirm: async () => {
      try {
        await api.request(`${RULE_URL}/${encodeURIComponent(code)}`, {
          method: 'DELETE',
          query: { expected_version: rule.version },
          silent: true,
        });
      } catch (e) {
        const msg = e.code === 'CFG-4005' ? '系统内置规则不可删除，只能停用。'
          : e.code === 'CFG-4006' ? '该规则已被他人修改，请刷新后重试。'
            : e.code === 'CFG-4001' ? '规则不存在或已被删除。'
              : `${saveErrorText(e)}`;
        errBox.textContent = msg;
        errBox.classList.add('on');
        if (e.code === 'CFG-4006' || e.code === 'CFG-4001') onDone && onDone();
        return false;
      }
      ui.toast(`已删除规则：${code}`, 'ok');
      onDone && onDone();
      return true;
    },
  });

  // 命中次数是提示项而非前置条件：查不到就显示 —，**绝不禁用删除**
  api.request(IMPACT_URL(code), { silent: true })
    .then((d) => {
      const n = d && (d.hit_count_30d === undefined ? d.hit_count : d.hit_count_30d);
      impact.textContent = typeof n === 'number' ? `${n} 次` : '—';
    })
    .catch(() => { impact.textContent = '—'; });
}

/** 启停用的二次确认（BR-06-09：从停用切启用前提示"建议先在仿真页验证"）。 */
export function openToggleConfirm(rule, target, onDone) {
  const code = String(rule._id || rule.rule_code || '');
  const errBox = ui.h('div', { class: 'form-error' });
  const enable = target === 'enabled';
  ui.modal({
    title: enable ? '启用规则' : '停用规则',
    confirmText: enable ? '确认启用' : '确认停用',
    variant: enable ? 'primary' : 'warn',
    body: [
      ui.h('div', { class: 'kv-list' }, [
        ui.h('div', { class: 'kv' }, [
          ui.h('span', { class: 'k', text: '规则' }),
          ui.h('span', { class: 'v' }, [ui.h('span', { class: 'mono', text: code }),
            document.createTextNode(` ${rule.name || ''}`)]),
        ]),
        ui.h('div', { class: 'kv' }, [
          ui.h('span', { class: 'k', text: '状态变更' }),
          ui.h('span', { class: 'v', text:
            `${labelOf('rule_status', rule.status, rule.status)} → ${labelOf('rule_status', target, target)}（version+1）` }),
        ]),
      ]),
      enable
        ? ui.h('div', { class: 'impact-note', text:
          '建议先在仿真页验证（BR-06-09）。启用后本规则立即参与后续事件决策，规则缓存随即失效（AD-02）。' })
        : ui.h('div', { class: 'impact-note', text:
          '停用后本规则立即退出决策，规则缓存随即失效（AD-02）；系统内置规则只能停用、不能删除。' }),
      errBox,
    ],
    onConfirm: async () => {
      try {
        const r = await api.request(`${RULE_URL}/${encodeURIComponent(code)}/toggle`, {
          method: 'POST', body: { status: target, expected_version: rule.version }, silent: true,
        });
        if (r && r.changed === false) {
          ui.toast(`目标状态与当前一致，未产生新版本（${code}）。`, 'warn', 5000);
        } else {
          ui.toast(`${code} 已${enable ? '启用' : '停用'}`
            + `${r && r.version !== undefined ? `（v${r.version}）` : ''}`, 'ok');
        }
      } catch (e) {
        errBox.textContent = e.code === 'CFG-4006'
          ? `该规则已被他人修改（当前 v${(e.data && e.data.current_version) || '?'}），请刷新后重试。`
          : saveErrorText(e);
        errBox.classList.add('on');
        if (e.code === 'CFG-4006' || e.code === 'CFG-4001') onDone && onDone();
        return false;
      }
      onDone && onDone();
      return true;
    },
  });
}
