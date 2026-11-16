(function () {
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content;

  // Select-all checkbox for bulk actions
  document.querySelectorAll('[data-select-all]').forEach((box) => {
    box.addEventListener('change', () => {
      document.querySelectorAll(box.dataset.selectAll).forEach((c) => { c.checked = box.checked; });
    });
  });

  // Confirm dangerous actions
  document.querySelectorAll('[data-confirm]').forEach((el) => {
    el.addEventListener('click', (e) => { if (!confirm(el.dataset.confirm)) e.preventDefault(); });
  });

  // Live template preview
  document.querySelectorAll('[data-preview]').forEach((form) => {
    const out = document.querySelector(form.dataset.preview);
    const subject = form.querySelector('[name=subject]');
    const body = form.querySelector('[name=body]');
    if (!out || !body) return;
    let timer;
    const run = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const res = await fetch('/ui/preview', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
          body: JSON.stringify({ subject: subject ? subject.value : '', body: body.value,
            contact_id: form.dataset.contact || null }),
        });
        if (!res.ok) return;
        const d = await res.json();
        out.innerHTML = '';
        const who = document.createElement('div');
        who.className = 'muted small';
        who.textContent = 'Preview for ' + d.contact;
        const subj = document.createElement('div');
        subj.className = 'subj';
        subj.textContent = d.subject || '(threaded reply — uses previous subject)';
        const txt = document.createElement('div');
        txt.textContent = d.body + (d.signature ? '\n\n' + d.signature : '');
        out.append(who, subj, txt);
        if (d.missing.length) {
          const warn = document.createElement('div');
          warn.className = 'flash warn small';
          warn.style.marginTop = '8px';
          warn.textContent = 'Missing for this contact (step would pause): ' + d.missing.join(', ') +
            ' — add a fallback like {{' + d.missing[0] + '|there}}';
          out.append(warn);
        }
      }, 250);
    };
    [subject, body].forEach((el) => el && el.addEventListener('input', run));
    run();
  });

  // Click a merge-field chip to insert it at the cursor
  document.querySelectorAll('.fields code').forEach((chip) => {
    chip.addEventListener('click', () => {
      const target = document.querySelector(chip.closest('.fields').dataset.target);
      if (!target) return;
      const v = '{{' + chip.textContent + '}}';
      const s = target.selectionStart ?? target.value.length;
      target.value = target.value.slice(0, s) + v + target.value.slice(target.selectionEnd ?? s);
      target.focus();
      target.dispatchEvent(new Event('input'));
    });
  });

  // Step editor: show/hide fields by step type
  document.querySelectorAll('[data-step-form]').forEach((form) => {
    const type = form.querySelector('[name=step_type]');
    const sync = () => {
      const isEmail = type.value === 'email';
      form.querySelectorAll('.email-only').forEach((el) => { el.hidden = !isEmail; });
      form.querySelectorAll('.manual-only').forEach((el) => { el.hidden = isEmail; });
    };
    type.addEventListener('change', sync);
    sync();
  });

  // Kanban drag & drop
  let dragged = null;
  document.querySelectorAll('.deal[draggable]').forEach((card) => {
    card.addEventListener('dragstart', () => { dragged = card; card.style.opacity = '.5'; });
    card.addEventListener('dragend', () => { card.style.opacity = ''; dragged = null; });
  });
  document.querySelectorAll('.col[data-stage]').forEach((col) => {
    col.addEventListener('dragover', (e) => { e.preventDefault(); col.classList.add('over'); });
    col.addEventListener('dragleave', () => col.classList.remove('over'));
    col.addEventListener('drop', async (e) => {
      e.preventDefault();
      col.classList.remove('over');
      if (!dragged) return;
      const card = dragged;
      const from = card.closest('.col');
      col.querySelector('.cards').appendChild(card);
      const res = await fetch('/ui/deals/' + card.dataset.id + '/stage', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
        body: JSON.stringify({ stage: col.dataset.stage }),
      });
      if (!res.ok) { from.querySelector('.cards').appendChild(card); alert('Could not move deal'); return; }
      const d = await res.json();
      document.querySelectorAll('.col[data-stage]').forEach((c) => {
        const s = d.summary.stages.find((x) => x.stage === c.dataset.stage);
        const tot = c.querySelector('[data-total]');
        if (s && tot) tot.textContent = '$' + Math.round(s.value).toLocaleString() + ' · ' + s.n;
      });
      const w = document.querySelector('[data-weighted]');
      if (w) w.textContent = '$' + Math.round(d.summary.weighted).toLocaleString();
      const o = document.querySelector('[data-open]');
      if (o) o.textContent = '$' + Math.round(d.summary.open_value).toLocaleString();
    });
  });
})();
