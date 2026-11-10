(() => {
    'use strict';

    const configElement = document.getElementById('liveTutorialConfig');
    if (!configElement) return;
    const config = JSON.parse(configElement.textContent || '{}');
    const storageKey = 'billcalc-live-tutorial-step-v1';
    const steps = [
        {
            endpoint: 'index', selector: '[data-tutorial-nav="tutorial"]', action: true,
            title: '나중에도 여기서 다시 볼 수 있어요',
            description: '왼쪽 메뉴의 ‘사용 안내’가 튜토리얼을 다시 여는 위치입니다. 지금 실제 메뉴를 직접 눌러 위치를 익혀보세요.',
            concept: '완료한 뒤에는 자동으로 나타나지 않지만, 이 메뉴는 계속 남아 있습니다.'
        },
        {
            endpoint: 'index', selector: '[data-tutorial-nav="settings"]', action: true,
            title: '먼저 층과 세대를 설정합니다',
            description: '전기요금을 나눌 기준이 되는 층·세대 정보를 먼저 만듭니다. 실제 ‘설정’ 메뉴를 눌러 이동하세요.',
            concept: '공실, 거주 인원, 할인 대상, TV 보유 여부도 세대 정보에 포함됩니다.'
        },
        {
            endpoint: 'settings', selector: '[data-tutorial-target="unit-management"] .content-card',
            title: '이 구성이 모든 계산의 기준입니다',
            description: '실제 설정 화면입니다. 층마다 전기 계약을 구분하고, 그 아래에 요금을 나눌 세대를 등록합니다.',
            concept: '세대 정보가 바뀌면 다음 계산부터 반영됩니다. 이미 저장된 과거 계산은 당시 정보의 복사본을 보존합니다.'
        },
        {
            endpoint: 'settings', selector: '[data-tutorial-nav="calculator"]', action: true,
            title: '세대 구성을 마치면 요금을 계산합니다',
            description: '실제 ‘요금 계산’ 메뉴를 눌러 검침값을 입력하는 화면으로 이동하세요.',
            concept: '설정 → 요금 계산 → 계산 내역 → 정산서 → 납부 관리 순서로 사용합니다.'
        },
        {
            endpoint: 'calculator', selector: '[data-tutorial-target="meter-readings"]', prepare: prepareCalculator,
            title: '전월과 현월 검침의 차이가 사용량입니다',
            description: '연습용 값을 실제 입력칸에 미리 채웠습니다. 값을 바꿔보면 오른쪽 사용량이 즉시 달라집니다.',
            concept: '현월 검침 − 전월 검침 = 이번 사용량. 전체 사용량에서 각 세대가 차지한 비율로 전기요금을 나눕니다.'
        },
        {
            endpoint: 'calculator', selector: '[data-tutorial-target="electric-save"]', action: true, prepare: prepareCalculator,
            title: '저장 전에 배분 결과를 먼저 확인합니다',
            description: '강조된 실제 ‘전기요금 저장’ 버튼을 누르세요. 바로 저장하지 않고 세대별 계산 결과를 먼저 보여줍니다.',
            concept: '할인 대상 세대가 없으면 해당 층의 재실 세대 전체에 할인액을 균등하게 나눕니다.'
        },
        {
            endpoint: 'calculator', selector: '[data-tutorial-target="calculation-confirm"]', action: true, modal: true,
            afterDelay: 1200,
            title: '검증된 결과만 연습 데이터에 저장합니다',
            description: '세대별 기본 배분, 할인, 최종 청구액을 확인한 뒤 실제 저장 버튼을 누르세요.',
            concept: '이 저장은 격리된 연습 데이터에만 반영되며 실제 사용자 데이터에는 영향을 주지 않습니다.'
        },
        {
            endpoint: 'view_bills', selector: '[data-tutorial-target="electric-detail"]', action: true,
            title: '계산 결과는 계산 내역에 쌓입니다',
            description: '방금 저장한 계산을 포함한 실제 목록입니다. 첫 번째 전기요금의 ‘상세’를 눌러 배분 근거를 확인하세요.',
            concept: '계산 내역은 정산서에 넣을 재료입니다. 아직 세대에 청구할 정산서는 아닙니다.'
        },
        {
            endpoint: 'view_electric_detail', selector: '[data-tutorial-target="electric-result"] tbody tr',
            title: '검침값부터 최종 청구액까지 추적할 수 있습니다',
            description: '세대별 전월·현월 검침, 사용량, 사용량 비례 배분액과 할인 내역이 한 표에 남습니다.',
            concept: '검침 기록과 계산 결과가 함께 보존되어 나중에도 금액의 근거를 확인할 수 있습니다.'
        },
        {
            endpoint: 'view_electric_detail', selector: '[data-tutorial-nav="invoice_combination"]', action: true,
            overridePath: '/invoice?tab=history',
            title: '계산 결과를 모아 정산서를 만듭니다',
            description: '실제 ‘정산서’ 메뉴를 누르세요. 연습 데이터에 이미 만들어 둔 정산서 내역을 확인합니다.',
            concept: '전기요금은 매월 계산해도 세대 청구 주기는 다를 수 있으므로 서로 다른 정산월의 결과를 한 정산서에 묶을 수 있습니다.'
        },
        {
            endpoint: 'invoice_combination', selector: '[data-tutorial-target="invoice-history"] table',
            title: '정산서는 세대에 청구할 항목 묶음입니다',
            description: '계산된 전기·수도·공동 공과금과 세대별 추가 금액을 골라 하나의 정산서로 발행합니다.',
            concept: '계산 결과 = 배분된 요금 재료, 정산서 = 실제 청구할 재료 묶음. 두 단계를 구분하면 과거 기록을 안전하게 관리할 수 있습니다.',
            nextPath: '/payments'
        },
        {
            endpoint: 'payments', selector: '[data-tutorial-target="payment-unit"]', action: true,
            title: '마지막으로 세대별 입금을 기록합니다',
            description: '실제 세대 행을 눌러 정산서별 고지액, 입금액과 남은 잔액을 불러오세요.',
            concept: '입금은 정산서와 연결되어 누적 잔액에 반영됩니다. 이월 항목이 있어도 잔액은 중복 계산되지 않습니다.'
        },
        {
            endpoint: 'payments', selector: '[data-tutorial-target="payment-history"] .summary-card', final: true,
            title: '전체 사용 흐름을 완료했습니다',
            description: '층·세대 설정부터 검침 계산, 정산서 묶음, 납부 확인까지 실제 화면으로 연습했습니다.',
            concept: '완료하면 연습 데이터는 모두 삭제되고 실제 데이터를 입력할 수 있는 상태로 돌아갑니다.'
        }
    ];

    const guide = document.getElementById('tutorialGuide');
    const masks = {
        top: document.querySelector('.tutorial-mask-top'),
        right: document.querySelector('.tutorial-mask-right'),
        bottom: document.querySelector('.tutorial-mask-bottom'),
        left: document.querySelector('.tutorial-mask-left')
    };
    const spotlight = document.querySelector('.tutorial-spotlight');
    const title = document.getElementById('tutorialTitle');
    const description = document.getElementById('tutorialDescription');
    const concept = document.getElementById('tutorialConcept');
    const count = document.getElementById('tutorialStepCount');
    const progress = document.getElementById('tutorialProgressBar');
    const hint = document.getElementById('tutorialTargetHint');
    const actions = document.getElementById('tutorialActions');
    let targetCleanup = null;
    let renderTimer = null;
    let missingModalAttempts = 0;

    function readStep() {
        const parsed = Number.parseInt(localStorage.getItem(storageKey) || '0', 10);
        return Number.isInteger(parsed) && parsed >= 0 && parsed < steps.length ? parsed : 0;
    }

    function writeStep(index) {
        localStorage.setItem(storageKey, String(Math.max(0, Math.min(index, steps.length - 1))));
    }

    function controlUrl(action, nextAction) {
        const url = new URL(`${config.controlBase}/${action}`);
        url.searchParams.set('token', config.controlToken);
        if (nextAction) url.searchParams.set('next_action', nextAction);
        return url.toString();
    }

    function setBox(element, left, top, width, height) {
        element.style.left = `${Math.max(0, left)}px`;
        element.style.top = `${Math.max(0, top)}px`;
        element.style.width = `${Math.max(0, width)}px`;
        element.style.height = `${Math.max(0, height)}px`;
        element.style.right = 'auto';
        element.style.bottom = 'auto';
    }

    function dimAround(target) {
        const viewportWidth = window.innerWidth;
        const viewportHeight = window.innerHeight;
        if (!target) {
            setBox(masks.top, 0, 0, viewportWidth, viewportHeight);
            setBox(masks.right, 0, 0, 0, 0);
            setBox(masks.bottom, 0, 0, 0, 0);
            setBox(masks.left, 0, 0, 0, 0);
            spotlight.hidden = true;
            return;
        }
        const rect = target.getBoundingClientRect();
        const padding = 8;
        const left = Math.max(0, rect.left - padding);
        const top = Math.max(0, rect.top - padding);
        const right = Math.min(viewportWidth, rect.right + padding);
        const bottom = Math.min(viewportHeight, rect.bottom + padding);
        setBox(masks.top, 0, 0, viewportWidth, top);
        setBox(masks.bottom, 0, bottom, viewportWidth, viewportHeight - bottom);
        setBox(masks.left, 0, top, left, bottom - top);
        setBox(masks.right, right, top, viewportWidth - right, bottom - top);
        setBox(spotlight, left, top, right - left, bottom - top);
        spotlight.hidden = false;
    }

    function openNavigationFor(target) {
        if (!target?.closest('.sidebar') || window.innerWidth > 768) return;
        document.body.classList.add('nav-open');
        const backdrop = document.getElementById('navBackdrop');
        const toggle = document.getElementById('menuToggle');
        if (backdrop) backdrop.hidden = false;
        if (toggle) toggle.setAttribute('aria-expanded', 'true');
    }

    function targetIsVisible(target, step) {
        if (!target || target.hidden) return false;
        if (step.modal && !target.closest('.modal.active')) return false;
        const rect = target.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
    }

    async function prepareCalculator() {
        const form = document.getElementById('electricForm');
        if (form?.dataset.tutorialPrepared === 'true') return;
        const floor = document.getElementById('electric_floor_id');
        const billingMonth = document.getElementById('billing_month');
        if (!floor || !billingMonth || floor.options.length < 2) return;
        billingMonth.value = '2026-10';
        if (!floor.value) {
            floor.value = floor.options[1].value;
            floor.dispatchEvent(new Event('change', {bubbles: true}));
        }
        const monthRow = document.querySelector('#monthlyBillsContainer .month-bill-row');
        if (monthRow) {
            monthRow.querySelector('input[type="month"]').value = '2026-10';
            monthRow.querySelector('.bill-amount').value = '248000';
            monthRow.querySelector('.bill-welfare').value = '16000';
            monthRow.querySelector('.bill-voucher').value = '8000';
            monthRow.querySelector('.bill-tv-fee').value = '7500';
            monthRow.querySelectorAll('input').forEach(input => input.dispatchEvent(new Event('input', {bubbles: true})));
        }
        await new Promise(resolve => window.setTimeout(resolve, 30));
        let previous = {};
        try {
            const response = await fetch(`/get_previous_readings/${floor.value}/${billingMonth.value}`);
            const result = await response.json();
            if (result.success) previous = result.readings || {};
        } catch (_error) {
            previous = {};
        }
        document.querySelectorAll('.reading-prev').forEach((input, index) => {
            const base = Number(previous[input.dataset.unit] ?? 1000 + index * 250);
            input.value = base.toFixed(2);
            const current = document.querySelector(`.reading-curr[data-unit="${input.dataset.unit}"]`);
            if (current) current.value = (base + 75 + index * 18).toFixed(2);
            input.dispatchEvent(new Event('input', {bubbles: true}));
            current?.dispatchEvent(new Event('input', {bubbles: true}));
        });
        if (form) form.dataset.tutorialPrepared = 'true';
    }

    function expectedEndpoint(step) {
        return config.endpoint === step.endpoint;
    }

    function endpointPath(step) {
        const paths = {
            index: '/', settings: '/settings', calculator: '/calculator',
            view_bills: '/view', invoice_combination: '/invoice?tab=history', payments: '/payments'
        };
        return paths[step.endpoint] || null;
    }

    function advance(step, index) {
        if (index >= steps.length - 1) return;
        writeStep(index + 1);
        const destination = step.nextPath || endpointPath(steps[index + 1]);
        if (step.nextPath || (destination && steps[index + 1].endpoint !== config.endpoint)) {
            window.location.assign(destination);
            return;
        }
        scheduleRender(40);
    }

    function renderActions(step, index) {
        actions.innerHTML = '';
        if (step.final) {
            const complete = document.createElement('button');
            complete.className = 'btn btn-primary';
            complete.textContent = config.hadUserData ? '완료하고 실제 데이터로 돌아가기' : '완료하고 새 주택 설정하기';
            complete.addEventListener('click', () => {
                localStorage.removeItem(storageKey);
                window.location.assign(controlUrl('complete', 'setup'));
            });
            actions.appendChild(complete);
            if (!config.hadUserData) {
                const importButton = document.createElement('button');
                importButton.className = 'btn btn-secondary';
                importButton.textContent = '완료하고 백업 파일 가져오기';
                importButton.addEventListener('click', () => {
                    localStorage.removeItem(storageKey);
                    window.location.assign(controlUrl('complete', 'import'));
                });
                actions.appendChild(importButton);
            }
            return;
        }
        if (!step.action) {
            const next = document.createElement('button');
            next.className = 'btn btn-primary';
            next.textContent = index === 2 ? '세대 구성 이해했어요' : '다음 단계';
            next.addEventListener('click', () => advance(step, index));
            actions.appendChild(next);
        }
    }

    function bindTarget(target, step, index) {
        if (targetCleanup) targetCleanup();
        targetCleanup = null;
        if (!step.action) return;
        const click = event => {
            writeStep(index + 1);
            if (step.overridePath) {
                event.preventDefault();
                window.location.assign(step.overridePath);
                return;
            }
            window.setTimeout(() => scheduleRender(80), step.afterDelay || 80);
        };
        target.addEventListener('click', click, {capture: true, once: true});
        targetCleanup = () => target.removeEventListener('click', click, {capture: true});
    }

    async function render() {
        window.clearTimeout(renderTimer);
        const index = readStep();
        const step = steps[index];
        if (!expectedEndpoint(step)) {
            const destination = endpointPath(step);
            if (destination) window.location.replace(destination);
            else writeStep(0);
            return;
        }
        if (step.prepare) await step.prepare();
        if (index === 6) {
            const risk = document.getElementById('calculationRiskConfirm');
            if (risk && !risk.closest('[hidden]')) {
                risk.checked = true;
                risk.dispatchEvent(new Event('change', {bubbles: true}));
            }
        }

        title.textContent = step.title;
        description.textContent = step.description;
        concept.textContent = step.concept || '';
        concept.hidden = !step.concept;
        count.textContent = `${index + 1} / ${steps.length}`;
        progress.style.width = `${((index + 1) / steps.length) * 100}%`;
        hint.hidden = !step.action;
        renderActions(step, index);

        const target = document.querySelector(step.selector);
        if (!targetIsVisible(target, step)) {
            if (step.modal) {
                missingModalAttempts += 1;
                if (missingModalAttempts >= 20) {
                    missingModalAttempts = 0;
                    writeStep(Math.max(0, index - 1));
                    scheduleRender(40);
                    return;
                }
            }
            dimAround(null);
            scheduleRender(140);
            return;
        }
        missingModalAttempts = 0;
        openNavigationFor(target);
        target.scrollIntoView({behavior: 'auto', block: 'center', inline: 'nearest'});
        window.setTimeout(() => {
            dimAround(target);
            bindTarget(target, step, index);
        }, 180);
    }

    function scheduleRender(delay = 80) {
        window.clearTimeout(renderTimer);
        renderTimer = window.setTimeout(render, delay);
    }

    function deferTutorial() {
        window.location.assign(controlUrl('defer'));
    }
    document.getElementById('tutorialDefer').addEventListener('click', deferTutorial);
    document.getElementById('tutorialPause').addEventListener('click', deferTutorial);
    document.getElementById('tutorialRestart').addEventListener('click', () => {
        if (!window.confirm('연습 내용을 지우고 첫 단계부터 다시 시작할까요?')) return;
        localStorage.removeItem(storageKey);
        window.location.assign(controlUrl('restart'));
    });
    window.addEventListener('resize', () => scheduleRender(60));
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape') deferTutorial();
    });

    render();
})();
