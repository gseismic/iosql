/* =====================================================================
 * iosql Web · tab-maintain.js
 * Maintain 维护台（仅 --write 模式可用）：
 * 步骤流 layout_report → compact → validate → cleanup_obsolete(两步)
 * → repartition；危险区 delete / truncate 均需手输表名确认。
 * 只读模式下整 Tab 灰置并提示「只读模式下不可用」。
 * ===================================================================== */
(function () {
  'use strict';
  var IU = window.IU;
  var h = IU.h;

  IU.tabs = IU.tabs || {};

  IU.tabs.maintain = {
    id: 'maintain',
    label: 'Maintain',

    render: function (root, ctx) {
      IU.clear(root);

      if (ctx.mode !== 'write') {
        root.append(h('div', { class: 'locked-tip', title: '只读模式下不可用' },
          h('div', { style: { fontSize: '34px' } }, '🔒'),
          h('div', { style: { fontWeight: 700, marginTop: '8px' } }, '维护台在只读模式下不可用'),
          h('div', { style: { marginTop: '4px' } },
            'compact / cleanup_obsolete / repartition / delete / truncate 仅在 --write 模式下开放，'),
          h('div', null, '且只读模式下服务端不注册任何写端点。'),
          h('div', { class: 'cmd-hint' },
            h('span', null, 'iosql ui -s <库路径> --write'),
            h('button', {
              class: 'btn sm', title: '复制开启命令',
              onclick: function () {
                if (navigator.clipboard && navigator.clipboard.writeText) {
                  navigator.clipboard.writeText('iosql ui -s <库路径> --write')
                    .then(function () { IU.toast('开启命令已复制', 'ok'); });
                }
              }
            }, '复制'))));
        return;
      }

      var base = '/api/table/' + encodeURIComponent(ctx.table);
      root.append(
        stepLayoutReport(base),
        stepCompact(base, ctx),
        stepValidate(base),
        stepCleanup(base, ctx),
        stepRepartition(base, ctx),
        dangerZone(base, ctx));
    }
  };

  /* 步骤卡骨架：编号 + 标题 + 说明 + 控件区 + 结果区 */
  function stepCard(idx, title, desc, controls, resultBox) {
    return h('div', { class: 'card step-card' },
      h('div', { class: 'card-title' },
        h('span', { class: 'idx' }, String(idx)),
        h('span', { class: 'micro-label' }, title)),
      desc ? h('div', { class: 'card-desc' }, desc) : null,
      controls,
      resultBox);
  }

  function resultBoxEl() { return h('div', { class: 'result-area' }); }

  /* 结果 pill 行：[{k,v}]，v 为 null 显示 — */
  function pills(pairs) {
    return h('div', { class: 'result-kv' }, pairs.map(function (p) {
      return h('span', { class: 'result-pill' }, p.k + ': ',
        h('b', null, p.v === null || p.v === undefined ? '—' : String(p.v)));
    }));
  }

  /* 原始 JSON 折叠 */
  function rawDetails(obj) {
    if (!obj || typeof obj !== 'object') return null;
    return h('details', { class: 'stats-details' },
      h('summary', null, '原始返回 JSON'),
      h('pre', null, JSON.stringify(obj, null, 2)));
  }

  function busy(btn, on, busyText) {
    btn.disabled = !!on;
    if (on) { btn.dataset.label = btn.textContent; btn.textContent = busyText || '执行中…'; }
    else if (btn.dataset.label) { btn.textContent = btn.dataset.label; }
  }

  /* ---------- ① layout_report ---------- */
  function stepLayoutReport(base) {
    var box = resultBoxEl();
    var btn = h('button', {
      class: 'btn primary',
      onclick: function () {
        busy(btn, true);
        IU.api(base + '/layout_report').then(function (resp) {
          busy(btn, false);
          box.classList.add('done');
          renderReport(box, IU.unwrap(resp, 'report'));
        }).catch(function () {
          busy(btn, false);
          IU.clear(box);
          box.append(h('div', { class: 'empty-state' }, '报告获取失败'));
        });
      }
    }, '生成布局报告');

    function renderReport(box, report) {
      IU.clear(box);
      var reco = IU.pick(report || {}, 'recommendation', 'suggestion', 'advice');
      var kids = [];
      if (reco !== undefined && reco !== null && reco !== '') {
        kids.push(h('div', { class: 'reco-highlight' }, '⚡ 官方建议：', String(reco)));
      }
      // 标量字段全部展示（total_rows 等）；对象/数组折叠进原始 JSON
      var scalarPairs = [];
      Object.keys(report || {}).forEach(function (k) {
        var v = report[k];
        if (v === null || v === undefined || typeof v === 'object') return;
        if (k === 'recommendation' || k === 'suggestion' || k === 'advice') return;
        scalarPairs.push({ k: k, v: k.indexOf('bytes') >= 0 ? IU.fmt.bytes(v) : v });
      });
      if (scalarPairs.length) kids.push(h('div', { style: { marginTop: '8px' } }, pills(scalarPairs)));
      kids.push(rawDetails(report));
      box.append.apply(box, kids);
    }

    return stepCard(1, '布局报告 · layout_report',
      '诊断分区行数分布，官方 recommendation 直接高亮展示。只读操作。',
      h('div', { class: 'inline-form' }, btn), box);
  }

  /* ---------- ② compact ---------- */
  function stepCompact(base, ctx) {
    var box = resultBoxEl();
    var partitionIn = h('input', { class: 'input mono', placeholder: 'partition 键（可选）', autocomplete: 'off' });
    var targetRowsIn = h('input', { class: 'input mono', placeholder: 'target_rows（可选）', autocomplete: 'off' });
    var btn = h('button', {
      class: 'btn danger',
      onclick: function () {
        IU.confirmModal({
          title: '执行 Compact？',
          message: '将按 order_key/time 重排当前可见 segment 并写入新 generation；旧 segment 标记为 obsolete（文件暂保留，可用 cleanup_obsolete 回收）。',
          confirmText: '执行 compact',
          danger: true,
          typeName: ctx.table
        }).then(function (ok) {
          if (!ok) return;
          busy(btn, true);
          var body = {};
          if (partitionIn.value.trim() !== '') body.partition = partitionIn.value.trim();
          if (targetRowsIn.value.trim() !== '') body.target_rows = Number(targetRowsIn.value.trim());
          IU.api(base + '/compact', { method: 'POST', body: body }).then(function (resp) {
            busy(btn, false);
            IU.clear(box);
            var r = IU.unwrap(resp, 'result') || {};
            box.append(pills([
              { k: 'rows_read', v: IU.pick(r, 'rows_read', 'rowsSeen') },
              { k: 'rows_written', v: IU.pick(r, 'rows_written', 'rows_written') },
              { k: 'new_segments', v: IU.pick(r, 'new_segments', 'new_segments') }
            ]), rawDetails(resp));
            IU.toast('compact 完成', 'ok');
          }).catch(function () { busy(btn, false); });
        });
      }
    }, '执行 Compact');

    return stepCard(2, '整理 Segment · compact',
      '合并碎片化 segment。需要手输表名确认。',
      h('div', { class: 'inline-form' },
        h('label', null, 'partition', partitionIn),
        h('label', null, 'target_rows', targetRowsIn),
        btn),
      box);
  }

  /* ---------- ③ validate ---------- */
  function stepValidate(base) {
    var box = resultBoxEl();
    var deepChk = h('input', { type: 'checkbox', id: 'val-deep' });
    var btn = h('button', {
      class: 'btn primary',
      onclick: function () {
        busy(btn, true);
        IU.api(base + '/validate', { method: 'POST', body: { deep: deepChk.checked } }).then(function (resp) {
          busy(btn, false);
          IU.clear(box);
          var v = IU.unwrap(resp, 'validation') || {};
          var ok = !!(IU.pick(v, 'ok', 'valid', 'passed'));
          box.append(
            h('div', null, ok ? IU.badge('校验通过 OK', 'ok') : IU.badge('校验失败 FAILED', 'err')),
            buildErrors(v));
        }).catch(function () { busy(btn, false); });
      }
    }, '执行校验');

    function buildErrors(resp) {
      var errors = IU.pick(resp || {}, 'errors', 'problems');
      if (!Array.isArray(errors) || !errors.length) {
        return h('div', { class: 'dim', style: { marginTop: '6px' } }, '无错误');
      }
      return h('ul', { class: 'error-list' }, errors.map(function (e) {
        return h('li', null, typeof e === 'string' ? e : JSON.stringify(e));
      }));
    }

    return stepCard(3, '一致性校验 · validate',
      'deep 校验会逐 segment 检查 SQLite integrity / row_count / manifest stats。',
      h('div', { class: 'inline-form' },
        h('label', { for: 'val-deep', style: { display: 'flex', gap: '6px', alignItems: 'center', cursor: 'pointer', fontSize: '12px', color: 'var(--tx1)' } },
          deepChk, 'deep 深度校验'),
        btn),
      box);
  }

  /* ---------- ④ cleanup_obsolete（强制两步） ---------- */
  function stepCleanup(base, ctx) {
    var box = resultBoxEl();
    var preview = null; // dry-run 结果缓存
    var partitionIn = h('input', { class: 'input mono', placeholder: 'partition 键（可选）', autocomplete: 'off' });

    var confirmInput = h('input', {
      class: 'input mono', placeholder: '输入表名 "' + ctx.table + '" 解锁删除按钮',
      autocomplete: 'off', spellcheck: 'false'
    });
    var deleteBtn = h('button', { class: 'btn danger', disabled: true, onclick: doDelete }, '确认删除');
    confirmInput.addEventListener('input', function () {
      deleteBtn.disabled = confirmInput.value.trim() !== ctx.table;
    });

    var previewBtn = h('button', {
      class: 'btn primary',
      onclick: function () {
        busy(previewBtn, true);
        IU.api(base + '/cleanup_obsolete', { method: 'POST', body: cleanupBody(true) }).then(function (resp) {
          busy(previewBtn, false);
          preview = IU.unwrap(resp, 'result') || {};
          renderPreview(preview);
        }).catch(function () { busy(previewBtn, false); });
      }
    }, '① 预览可回收空间');

    function cleanupBody(dryRun) {
      var body = { dry_run: dryRun };
      if (partitionIn.value.trim() !== '') body.partition = partitionIn.value.trim();
      return body;
    }

    function renderPreview(resp) {
      IU.clear(box);
      var bytes = IU.pick(resp || {}, 'bytes_reclaimable', 'bytes');
      var files = IU.pick(resp || {}, 'files', 'candidates') ;
      var fileCount = Array.isArray(files) ? files.length : (typeof files === 'number' ? files : undefined);

      box.append(
        pills([
          { k: 'bytes_reclaimable', v: bytes === undefined ? undefined : IU.fmt.bytes(bytes) },
          { k: 'files', v: fileCount }
        ]),
        Array.isArray(files) && files.length
          ? h('details', { class: 'stats-details' }, h('summary', null, '候选文件清单'),
              h('pre', null, files.map(function (f) { return typeof f === 'string' ? f : JSON.stringify(f); }).join('\n')))
          : null,
        rawDetails(resp),
        h('div', { class: 'inline-form', style: { marginTop: '10px' } },
          confirmInput, deleteBtn,
          h('span', { class: 'faint', style: { fontSize: '11px' } }, '删除前服务端会先做深度校验，失败即拒绝')));
    }

    function doDelete() {
      if (!preview) return;
      IU.confirmModal({
        title: '物理删除 obsolete segment？',
        message: '此操作将永久删除预览列出的旧文件，不可撤销。',
        confirmText: '② 确认物理删除',
        danger: true,
        typeName: ctx.table
      }).then(function (ok) {
        if (!ok) return;
        busy(deleteBtn, true);
        IU.api(base + '/cleanup_obsolete', { method: 'POST', body: cleanupBody(false) }).then(function (resp) {
          busy(deleteBtn, false);
          preview = null;
          IU.clear(box);
          var r = IU.unwrap(resp, 'result') || {};
          box.append(pills([
            { k: 'files_deleted', v: IU.pick(r, 'files_deleted', 'deleted_files') },
            { k: 'bytes_deleted', v: fmtDeletedBytes(r) }
          ]), rawDetails(resp));
          IU.toast('obsolete 清理完成', 'ok');
        }).catch(function () { busy(deleteBtn, false); });
      });
    }

    function fmtDeletedBytes(resp) {
      var v = IU.pick(resp || {}, 'bytes_deleted', 'deleted_bytes');
      return v === undefined ? undefined : IU.fmt.bytes(v);
    }

    return stepCard(4, '清理废弃段 · cleanup_obsolete',
      '强制两步：先 dry-run 预览 bytes_reclaimable 与候选文件，再手输表名解锁真实删除。',
      h('div', { class: 'inline-form' },
        h('label', null, 'partition', partitionIn),
        previewBtn),
      box);
  }

  /* ---------- ⑤ repartition ---------- */
  function stepRepartition(base, ctx) {
    var box = resultBoxEl();
    var targetUriIn = h('input', {
      class: 'input mono', placeholder: '目标库 URI，如 sqlite://database/market-hourly.iosql',
      autocomplete: 'off', spellcheck: 'false', style: { flex: '1 1 320px' }
    });
    var partitionStrIn = h('input', {
      class: 'input mono', placeholder: "duration(ts,every=1hour)",
      autocomplete: 'off', spellcheck: 'false', style: { flex: '1 1 260px' }
    });

    function bodyOf() {
      return {
        target_uri: targetUriIn.value.trim(),
        partition_str: partitionStrIn.value.trim()
      };
    }

    function validInputs() {
      if (targetUriIn.value.trim() === '') { IU.toast('target_uri 必填（重分区永不原地改源库）', 'warn'); return false; }
      if (partitionStrIn.value.trim() === '') { IU.toast('partition_str 必填', 'warn'); return false; }
      return true;
    }

    var runBtn = h('button', {
      class: 'btn danger',
      onclick: function () {
        if (!validInputs()) return;
        IU.confirmModal({
          title: '执行重分区（写入新库）？',
          message: '将在新库「' + targetUriIn.value.trim() + '」按「' + partitionStrIn.value.trim() + '」创建新布局并复制全部行。',
          note: '服务端无预览/ dry-run：确认后立即写入新库。源库不变，源表数据不会被修改或删除。',
          confirmText: '执行重分区（写入新库）',
          danger: true,
          typeName: ctx.table
        }).then(function (ok) {
          if (!ok) return;
          busy(runBtn, true);
          IU.api(base + '/repartition', { method: 'POST', body: bodyOf() }).then(function (resp) {
            busy(runBtn, false);
            renderResult(resp);
            IU.toast('重分区完成', 'ok');
          }).catch(function () { busy(runBtn, false); });
        });
      }
    }, '执行重分区（写入新库）');

    function renderResult(resp) {
      IU.clear(box);
      var r = IU.unwrap(resp, 'result') || {};
      box.append(pills([
        { k: 'rows_copied', v: IU.pick(r, 'rows_copied', 'rowsCopied') },
        { k: 'partitions_created', v: IU.pick(r, 'partitions_created', 'partitionsCreated') },
        { k: 'target_uri', v: IU.pick(r, 'target_uri', 'targetUri') }
      ]), rawDetails(resp));
    }

    return stepCard(5, '重分区 · repartition',
      '把表按新 partition 策略写入目标新库并复制全部行（源库不变）。'
      + '服务端没有 dry-run 预览，需手输表名确认后直接写入；本操作不可预览。',
      h('div', { class: 'inline-form' },
        h('label', { style: { flex: '1 1 320px' } }, 'target_uri', targetUriIn),
        h('label', { style: { flex: '1 1 260px' } }, 'partition_str', partitionStrIn),
        runBtn),
      box);
  }

  /* ---------- 危险区：delete / truncate ---------- */
  function dangerZone(base, ctx) {
    var spec = ctx.spec || {};
    var colTypes = (spec.columns && typeof spec.columns === 'object') ? spec.columns : {};
    var columns = Object.keys(colTypes);
    var builder = columns.length ? new IU.ConditionBuilder(columns, colTypes) : null;

    var countSlot = h('span', { class: 'mono dim' }, '');
    var delResult = resultBoxEl();

    var countBtn = h('button', {
      class: 'btn sm',
      onclick: function () {
        var where = getWhere();
        if (where === undefined) return;
        IU.api(base + '/count', { method: 'POST', body: { where: where || [] } }).then(function (resp) {
          var n = IU.pick(resp || {}, 'count', 'total', 'rows');
          countSlot.textContent = '命中 ' + (n === undefined ? '?' : IU.fmt.int(n)) + ' 行';
        }).catch(function () { });
      }
    }, '统计命中');

    var delBtn = h('button', {
      class: 'btn danger',
      onclick: function () {
        var where = getWhere();
        if (where === undefined) return;
        if (!where || !where.length) { IU.toast('delete 的 where 不能为空', 'warn'); return; }
        IU.confirmModal({
          title: '按条件删除数据？',
          message: '将删除满足当前 where 条件的所有行。建议先用「统计命中」核对影响范围。',
          confirmText: '确认删除',
          danger: true,
          typeName: ctx.table
        }).then(function (ok) {
          if (!ok) return;
          busy(delBtn, true);
          IU.api(base + '/delete', { method: 'POST', body: { where: where } }).then(function (resp) {
            busy(delBtn, false);
            IU.clear(delResult);
            delResult.append(pills([
              { k: 'deleted', v: IU.pick(resp || {}, 'deleted', 'rows_deleted', 'count') }
            ]), rawDetails(resp));
            IU.toast('条件删除完成，建议刷新 Browse Data 核对', 'ok');
          }).catch(function () { busy(delBtn, false); });
        });
      }
    }, '按条件删除…');

    var truncBtn = h('button', {
      class: 'btn danger',
      onclick: function () {
        IU.confirmModal({
          title: '清空整张表？',
          message: 'truncate 将删除表「' + ctx.table + '」的全部数据与布局文件，契约保留。',
          confirmText: '确认清空',
          danger: true,
          typeName: ctx.table
        }).then(function (ok) {
          if (!ok) return;
          busy(truncBtn, true);
          IU.api(base + '/truncate', { method: 'POST', body: { confirm: ctx.table } }).then(function (resp) {
            busy(truncBtn, false);
            IU.toast('表已清空', 'ok');
          }).catch(function () { busy(truncBtn, false); });
        });
      }
    }, '清空整表…');

    function getWhere() {
      if (!builder) return [];
      var r = builder.getWhere();
      if (!r.ok) { IU.toast(r.msg, 'err'); return undefined; }
      return r.where;
    }

    return h('div', { class: 'card step-card', style: { borderLeftColor: '#a03030' } },
      h('div', { class: 'card-title' },
        h('span', { class: 'idx' }, '⚠'),
        h('span', { class: 'micro-label' }, '危险操作 · Delete / Truncate'),
        IU.badge('需手输表名确认', 'err')),
      builder ? h('div', { style: { marginBottom: '10px' } }, builder.el) : null,
      h('div', { class: 'inline-form' }, countBtn, countSlot, delBtn, truncBtn),
      delResult);
  }
})();
