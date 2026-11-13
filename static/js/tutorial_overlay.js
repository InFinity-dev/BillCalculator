(() => {
    'use strict';

    const configElement = document.getElementById('liveTutorialConfig');
    if (!configElement) return;
    const config = JSON.parse(configElement.textContent || '{}');
    const runId = String(config.runId || config.controlToken || 'current');
    const storageKey = `billcalc-live-tutorial-step-v5:${runId}`;
    const modeStorageKey = `billcalc-live-tutorial-mode-v2:${runId}`;
    const legacyStorageKeys = [
        'billcalc-live-tutorial-step-v4',
        'billcalc-live-tutorial-step-v3',
        'billcalc-live-tutorial-mode-v1'
    ];
    const navigationStepParameter = '_tutorial_step';

    // 새 연습은 항상 첫 단계와 기본 데모 데이터에서 시작한다. 이전 버전의
    // 브라우저 진행 상태는 현재 일회성 실행으로 가져오지 않는다.
    legacyStorageKeys.forEach(key => localStorage.removeItem(key));
    const steps = [
        {
            endpoint: 'index', selector: '[data-tutorial-nav="tutorial"]', action: true,
            cue: '사용 안내는 여기서 언제든 다시 시작할 수 있어요',
            title: '나중에도 여기서 다시 볼 수 있어요',
            description: '왼쪽 메뉴의 ‘사용 안내’가 튜토리얼을 다시 여는 위치입니다. 지금 실제 메뉴를 직접 눌러 위치를 익혀보세요.',
            concept: '완료한 뒤에는 자동으로 나타나지 않지만, 이 메뉴는 계속 남아 있습니다.'
        },
        {
            endpoint: 'index', selector: '[data-tutorial-nav="settings"]', action: true,
            cue: '설정을 눌러 층과 세대 구성을 확인하세요',
            title: '먼저 층과 세대를 설정합니다',
            description: '전기요금을 나눌 기준이 되는 층·세대 정보를 먼저 만듭니다. 실제 ‘설정’ 메뉴를 눌러 이동하세요.',
            concept: '공실, 거주 인원, 할인 대상, TV 보유 여부도 세대 정보에 포함됩니다.'
        },
        {
            endpoint: 'settings', selector: '[data-tutorial-target="unit-management"] .content-card',
            cue: '층과 세대 구성은 이 영역에서 관리합니다',
            title: '이 구성이 모든 계산의 기준입니다',
            description: '실제 설정 화면입니다. 층마다 전기 계약을 구분하고, 그 아래에 요금을 나눌 세대를 등록합니다.',
            concept: '세대 정보가 바뀌면 다음 계산부터 반영됩니다. 이미 저장된 과거 계산은 당시 정보의 복사본을 보존합니다.',
            nextLabel: '세대 구성 이해했어요'
        },
        {
            endpoint: 'settings', selector: '[data-tutorial-nav="calculator"]', action: true,
            cue: '요금 계산으로 이동하세요',
            title: '세대 구성을 마치면 요금을 계산합니다',
            description: '실제 ‘요금 계산’ 메뉴를 눌러 검침값을 입력하는 화면으로 이동하세요.',
            concept: '설정 → 요금 계산 → 계산 내역 → 정산서 → 납부 관리 순서로 사용합니다.'
        },
        {
            endpoint: 'calculator', selector: '[data-tutorial-target="meter-readings"]', prepare: prepareCalculator,
            cue: '전월·현월 검침값과 계산된 사용량을 확인하세요',
            title: '전월과 현월 검침의 차이가 사용량입니다',
            description: '연습용 값을 실제 입력칸에 미리 채웠습니다. 값을 바꿔보면 오른쪽 사용량이 즉시 달라집니다.',
            concept: '현월 검침 − 전월 검침 = 이번 사용량. 전체 사용량에서 각 세대가 차지한 비율로 전기요금을 나눕니다.'
        },
        {
            endpoint: 'calculator', selector: '[data-tutorial-target="electric-save"]', action: true, prepare: prepareCalculator,
            completionEvent: 'tutorial:calculation-preview-ready',
            cue: '배분 결과를 확인하려면 전기요금 저장을 누르세요',
            title: '저장 전에 배분 결과를 먼저 확인합니다',
            description: '강조된 실제 ‘전기요금 저장’ 버튼을 누르세요. 바로 저장하지 않고 세대별 계산 결과를 먼저 보여줍니다.',
            concept: '할인 대상 세대가 없으면 해당 층의 재실 세대 전체에 할인액을 균등하게 나눕니다.'
        },
        {
            endpoint: 'calculator', selector: '[data-tutorial-target="calculation-confirm"]', action: true, modal: true,
            completionEvent: 'tutorial:calculation-saved',
            cue: '세대별 금액을 확인한 뒤 저장하세요',
            title: '검증된 결과만 연습 데이터에 저장합니다',
            description: '세대별 기본 배분, 할인, 최종 청구액을 확인한 뒤 실제 저장 버튼을 누르세요.',
            concept: '이 저장은 격리된 연습 데이터에만 반영되며 실제 사용자 데이터에는 영향을 주지 않습니다.'
        },
        {
            endpoint: 'view_bills', selector: '[data-tutorial-target="electric-detail"]', action: true,
            cue: '상세를 눌러 계산 근거를 확인하세요',
            title: '계산 결과는 계산 내역에 쌓입니다',
            description: '방금 저장한 계산을 포함한 실제 목록입니다. 첫 번째 전기요금의 ‘상세’를 눌러 배분 근거를 확인하세요.',
            concept: '계산 내역은 정산서에 넣을 재료입니다. 아직 세대에 청구할 정산서는 아닙니다.'
        },
        {
            endpoint: 'view_electric_detail', selector: '[data-tutorial-target="electric-result"] tbody tr',
            cue: '세대별 검침과 배분 결과를 확인하세요',
            title: '검침값부터 최종 청구액까지 추적할 수 있습니다',
            description: '세대별 전월·현월 검침, 사용량, 사용량 비례 배분액과 할인 내역이 한 표에 남습니다.',
            concept: '검침 기록과 계산 결과가 함께 보존되어 나중에도 금액의 근거를 확인할 수 있습니다.'
        },
        {
            endpoint: 'view_electric_detail', selector: '[data-tutorial-nav="invoice_combination"]', action: true,
            cue: '정산서에서 계산 결과를 청구 단위로 묶으세요',
            overridePath: '/invoice',
            title: '계산 결과를 모아 정산서를 만듭니다',
            description: '실제 ‘정산서’ 메뉴를 누르세요. 이번에는 계산서를 고르는 단계부터 정산서를 직접 만들어 봅니다.',
            concept: '전기요금은 매월 계산해도 세대 청구 주기는 다를 수 있으므로 서로 다른 정산월의 결과를 한 정산서에 묶을 수 있습니다.'
        },
        {
            endpoint: 'invoice_combination', selector: '[data-tutorial-target="invoice-name"]', prepare: prepareInvoice,
            cue: '발행할 정산서 이름을 확인하세요',
            title: '정산서는 계산 결과를 묶은 실제 청구 단위입니다',
            description: '연습용 정산서 이름과 메모를 미리 입력했습니다. 아래에서는 아직 어떤 정산서에도 쓰이지 않은 계산 결과를 고릅니다.',
            concept: '계산 결과 = 요금 배분 재료, 정산서 = 세대에 실제로 청구할 재료 묶음입니다.',
            nextLabel: '청구 항목 선택하기'
        },
        {
            endpoint: 'invoice_combination', selector: '[data-electric-month-select="2026-09"]', action: true,
            prepare: prepareInvoice, completionEvent: 'tutorial:electric-month-selected',
            completionCondition: (_target, event) => event.detail?.month === '2026-09'
                && event.detail.selectedCount === event.detail.totalCount,
            cue: '9월의 모든 층을 한 번에 선택하세요',
            title: '9월 전기요금을 모든 층에서 선택합니다',
            description: '‘이 달 전 층 선택’을 눌러 지하 1층, 1층, 2층의 9월 전기요금 계산 결과를 모두 담으세요.',
            concept: '전기요금은 매월 층별로 계산합니다. 한 층이라도 빠지면 그 층 세대에는 해당 월 전기요금이 청구되지 않습니다.'
        },
        {
            endpoint: 'invoice_combination', selector: '[data-electric-month-select="2026-10"]', action: true,
            prepare: prepareInvoice, completionEvent: 'tutorial:electric-month-selected',
            completionCondition: (_target, event) => event.detail?.month === '2026-10'
                && event.detail.selectedCount === event.detail.totalCount,
            cue: '10월의 모든 층도 함께 선택하세요',
            title: '10월 전기요금도 모든 층에서 선택합니다',
            description: '같은 방법으로 지하 1층, 1층, 2층의 10월 전기요금 계산 결과를 모두 담으세요.',
            concept: '9월과 10월 전기요금 6건을 하나의 10월 정산서로 묶어, 전기 고지보다 긴 세대 청구 주기를 반영합니다.'
        },
        {
            endpoint: 'invoice_combination', selector: '.water-item[data-month^="2026-10"]', action: true,
            prepare: prepareInvoice, interactionEvent: 'change', completionCondition: target => target.checked,
            cue: '9~10월 사용분 수도요금을 선택하세요',
            title: '두 달치 수도요금을 같은 정산서에 담습니다',
            description: '수도요금은 건물 전체에 2개월 단위로 고지됩니다. 강조된 10월 수도요금을 선택하세요.',
            concept: '이 항목은 9~10월 사용분을 10월에 고지한 수도요금입니다. 전기 6건과 수도 1건을 함께 청구합니다.'
        },
        {
            endpoint: 'invoice_combination', selector: '[data-tutorial-target="invoice-next-items"]', action: true,
            prepare: prepareInvoice, completionEvent: 'tutorial:invoice-items-confirmed',
            cue: '세대별 설정으로 이동하세요',
            title: '선택한 항목으로 세대별 청구 내용을 준비합니다',
            description: '실제 ‘다음 단계: 세대별 설정’ 버튼을 눌러 선택 내용을 확정하세요.',
            concept: '한 번 정산서에 사용한 계산 결과는 다른 정산서에 중복으로 들어가지 않아 중복 청구를 막습니다.'
        },
        {
            endpoint: 'invoice_combination', selector: '[data-tutorial-target="invoice-next-review"]', action: true,
            prepare: prepareInvoice, completionEvent: 'tutorial:invoice-preview-ready',
            cue: '추가 금액을 확인하고 최종 확인으로 이동하세요',
            title: '세대별 추가 금액과 메모를 확인합니다',
            description: '미납금, 수리비, 환급액처럼 특정 세대에만 필요한 항목을 넣는 단계입니다. 연습에서는 그대로 ‘다음: 최종 확인’을 누르세요.',
            concept: '공통 계산 결과와 세대별 예외 금액을 분리해 두면 나중에도 청구 근거를 쉽게 확인할 수 있습니다.'
        },
        {
            endpoint: 'invoice_combination', selector: '[data-tutorial-target="invoice-create"]', action: true,
            prepare: prepareInvoice, requiresEnabled: true, completionEvent: 'tutorial:invoice-created',
            cue: '세대별 최종 금액을 확인한 뒤 정산서를 생성하세요',
            title: '최종 금액을 확인하고 정산서를 생성합니다',
            description: '항목별 정산월과 세대별 최종 청구액을 확인한 뒤 실제 ‘정산서 생성’ 버튼을 누르세요.',
            concept: '화면에 보이는 최종 확인 내용과 저장 내용이 달라지면 생성이 거부되어 금액 불일치를 방지합니다.'
        },
        {
            endpoint: 'view_invoice', selector: '[data-tutorial-target="invoice-print"]', action: true,
            cue: '인쇄용 보기에서 세대별 배부 형태를 확인하세요',
            title: '방금 만든 정산서가 실제 청구 기록으로 남았습니다',
            description: '세대별 금액과 포함 항목을 확인할 수 있습니다. 강조된 ‘인쇄용 보기’를 눌러 실제 배부 형태를 확인하세요.',
            concept: '계산 결과를 선택하고 세대별 예외를 반영한 최종 금액이 세대마다 한 장의 배부용 정산서로 만들어집니다.'
        },
        {
            endpoint: 'print_invoice', selector: '[data-tutorial-target="print-preview"]',
            cue: '첫 세대의 배부용 정산서를 확인하세요',
            title: '세대마다 한 장씩 인쇄됩니다',
            description: '세대명, 포함된 고지월, 항목별 금액과 최종 청구금액이 한 장에 정리됩니다.',
            concept: '정산서에는 모든 층의 9월·10월 전기요금과 9~10월 사용분 수도요금이 각 고지 정보를 유지한 채 함께 표시됩니다.',
            nextLabel: '인쇄 방법 확인하기'
        },
        {
            endpoint: 'print_invoice', selector: '[data-tutorial-target="print-action"]', action: true,
            completionEvent: 'tutorial:print-opened',
            skipActionLabel: '인쇄하지 않고 계속',
            cue: '인쇄하기를 눌러 출력 화면을 여세요',
            title: '운영체제 인쇄 창에서 출력합니다',
            description: '실제 ‘인쇄하기’를 누르거나, 지금 출력하지 않으려면 ‘인쇄하지 않고 계속’을 선택하세요.',
            concept: '인쇄 창에서 용지, 방향, 배율을 확인합니다. 안내 패널과 강조 표시는 출력물에 포함되지 않습니다.'
        },
        {
            endpoint: 'print_invoice', selector: '[data-tutorial-target="print-return"]', action: true,
            cue: '정산서로 돌아가 납부 관리를 이어가세요',
            title: '배부용 정산서 확인을 마쳤습니다',
            description: '인쇄 창을 닫은 뒤 강조된 ‘정산서로 돌아가기’를 눌러 원래 정산서 화면으로 이동하세요.',
            concept: '인쇄는 저장된 정산서를 출력할 뿐 계산 결과나 납부 상태를 변경하지 않습니다.'
        },
        {
            endpoint: 'view_invoice', selector: '[data-tutorial-target="invoice-payments"]', action: true,
            cue: '납부 내역으로 이동하세요',
            title: '이제 세대별 납부 상태를 확인합니다',
            description: '정산서 발행과 배부 형태를 확인했습니다. 강조된 실제 ‘납부 내역’ 버튼을 눌러 입금 관리로 이동하세요.',
            concept: '정산서의 고지액과 실제 입금액을 연결해 세대별 남은 잔액을 관리합니다.'
        },
        {
            endpoint: 'payments', selector: '[data-tutorial-target="payment-unit"]', action: true,
            completionEvent: 'tutorial:payment-history-ready',
            cue: '납부 현황을 확인할 세대를 선택하세요',
            title: '마지막으로 세대별 입금을 기록합니다',
            description: '실제 세대 행을 눌러 정산서별 고지액, 입금액과 남은 잔액을 불러오세요.',
            concept: '입금은 정산서와 연결되어 누적 잔액에 반영됩니다. 이월 항목이 있어도 잔액은 중복 계산되지 않습니다.'
        },
        {
            endpoint: 'payments', selector: '[data-tutorial-target="payment-history"] .summary-card', final: true,
            cue: '고지액·입금액·남은 잔액을 확인하세요',
            title: '전체 사용 흐름을 익혔어요',
            description: '층·세대 설정부터 검침 계산, 정산서 생성, 배부용 인쇄, 납부 확인까지 실제 화면으로 연습했습니다.',
            concept: '연습 데이터를 더 둘러보거나, 연습을 종료하고 실제 데이터 입력을 시작할 수 있습니다.'
        }
    ];

    const guide = document.getElementById('tutorialGuide');
    const focusRing = document.querySelector('.tutorial-focus-ring');
    const cue = document.getElementById('tutorialCue');
    const title = document.getElementById('tutorialTitle');
    const description = document.getElementById('tutorialDescription');
    const concept = document.getElementById('tutorialConcept');
    const count = document.getElementById('tutorialStepCount');
    const progress = document.getElementById('tutorialProgressBar');
    const hint = document.getElementById('tutorialTargetHint');
    const actions = document.getElementById('tutorialActions');
    const practiceDock = document.getElementById('tutorialPracticeDock');
    let targetCleanup = null;
    let trackingCleanup = null;
    let renderTimer = null;
    let missingModalAttempts = 0;
    let focusedTarget = null;
    let guardedTarget = null;
    let guardedStep = null;
    let guardedIndex = null;
    let reminderUntil = 0;
    let reminderMessage = '';

    function readStep() {
        const currentUrl = new URL(window.location.href);
        const navigationStep = Number.parseInt(currentUrl.searchParams.get(navigationStepParameter) || '', 10);
        if (Number.isInteger(navigationStep) && navigationStep >= 0 && navigationStep < steps.length) {
            localStorage.setItem(storageKey, String(navigationStep));
            currentUrl.searchParams.delete(navigationStepParameter);
            window.history.replaceState(
                window.history.state, '', `${currentUrl.pathname}${currentUrl.search}${currentUrl.hash}`
            );
            return navigationStep;
        }
        const stored = localStorage.getItem(storageKey);
        const parsed = Number.parseInt(stored || '0', 10);
        return Number.isInteger(parsed) && parsed >= 0 && parsed < steps.length ? parsed : 0;
    }

    function writeStep(index) {
        localStorage.setItem(storageKey, String(Math.max(0, Math.min(index, steps.length - 1))));
    }

    function isPracticeMode() {
        return localStorage.getItem(modeStorageKey) === 'practice';
    }

    function setPracticeMode(active) {
        if (active) localStorage.setItem(modeStorageKey, 'practice');
        else localStorage.removeItem(modeStorageKey);
    }

    function clearTutorialStorage() {
        localStorage.removeItem(storageKey);
        localStorage.removeItem(modeStorageKey);
        legacyStorageKeys.forEach(key => localStorage.removeItem(key));
    }

    function enterPracticeMode() {
        if (!window.confirm(
            '자유 연습으로 전환하면 단계 안내가 종료됩니다. 다시 안내받으려면 처음부터 시작해야 합니다. 전환할까요?'
        )) return;
        setPracticeMode(true);
        scheduleRender(0);
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

    function usableBounds() {
        const bounds = {left: 0, top: 0, right: window.innerWidth, bottom: window.innerHeight};
        const guideRect = guide.getBoundingClientRect();
        if (guideRect.width >= window.innerWidth * 0.7) {
            bounds.bottom = Math.max(0, guideRect.top);
        } else if (guideRect.height >= window.innerHeight * 0.7) {
            bounds.right = Math.max(0, guideRect.left);
        }
        return bounds;
    }

    function hideFocus() {
        focusRing.hidden = true;
        cue.hidden = true;
        focusedTarget = null;
    }

    function positionCue(rect, bounds, message) {
        cue.textContent = message;
        cue.hidden = false;
        cue.style.left = '0px';
        cue.style.top = '0px';
        const cueRect = cue.getBoundingClientRect();
        const gap = 14;
        const edge = 8;
        const centerLeft = rect.left + (rect.width - cueRect.width) / 2;
        const centerTop = rect.top + (rect.height - cueRect.height) / 2;
        const candidates = [];
        if (rect.width < (bounds.right - bounds.left) * 0.55) {
            candidates.push({placement: 'right', left: rect.right + gap, top: centerTop});
        }
        candidates.push(
            {placement: 'bottom', left: centerLeft, top: rect.bottom + gap},
            {placement: 'top', left: centerLeft, top: rect.top - cueRect.height - gap},
            {placement: 'left', left: rect.left - cueRect.width - gap, top: centerTop}
        );
        const fits = candidate => (
            candidate.left >= bounds.left + edge
            && candidate.top >= bounds.top + edge
            && candidate.left + cueRect.width <= bounds.right - edge
            && candidate.top + cueRect.height <= bounds.bottom - edge
        );
        const selected = candidates.find(fits) || {
            placement: 'bottom',
            left: Math.min(Math.max(centerLeft, bounds.left + edge), bounds.right - cueRect.width - edge),
            top: Math.min(Math.max(rect.bottom + gap, bounds.top + edge), bounds.bottom - cueRect.height - edge)
        };
        cue.dataset.placement = selected.placement;
        cue.style.left = `${Math.max(bounds.left + edge, selected.left)}px`;
        cue.style.top = `${Math.max(bounds.top + edge, selected.top)}px`;
    }

    function focusTarget(target, message) {
        const bounds = usableBounds();
        if (!target) {
            hideFocus();
            return;
        }

        const rect = target.getBoundingClientRect();
        const padding = 8;
        const left = Math.max(bounds.left, rect.left - padding);
        const top = Math.max(bounds.top, rect.top - padding);
        const right = Math.min(bounds.right, rect.right + padding);
        const bottom = Math.min(bounds.bottom, rect.bottom + padding);
        if (right <= left || bottom <= top) {
            hideFocus();
            return;
        }
        setBox(focusRing, left, top, right - left, bottom - top);
        focusRing.hidden = false;
        if (focusedTarget !== target) {
            focusRing.classList.remove('is-pulsing');
            void focusRing.offsetWidth;
            focusRing.classList.add('is-pulsing');
            focusedTarget = target;
        }
        positionCue({left, top, right, bottom, width: right - left, height: bottom - top}, bounds, message);
    }

    function openNavigationFor(target) {
        if (!target?.closest('.sidebar') || window.innerWidth > 768) return;
        document.body.classList.add('nav-open');
        const backdrop = document.getElementById('navBackdrop');
        const toggle = document.getElementById('menuToggle');
        if (backdrop) backdrop.hidden = false;
        if (toggle) toggle.setAttribute('aria-expanded', 'true');
    }

    function activeDialog() {
        const candidates = document.querySelectorAll(
            '.modal.active, dialog[open], [role="dialog"][aria-modal="true"][aria-hidden="false"]'
        );
        return [...candidates].find(dialog => {
            const style = window.getComputedStyle(dialog);
            const rect = dialog.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
                && rect.width > 0 && rect.height > 0;
        }) || null;
    }

    function syncDialogState() {
        const dialog = activeDialog();
        document.body.classList.toggle('tutorial-dialog-open', Boolean(dialog));
        return dialog;
    }

    function targetIsVisible(target, step) {
        if (!target || target.hidden || target.closest('[hidden]')) return false;
        if (step.modal && !target.closest(
            '.modal.active, dialog[open], [role="dialog"][aria-modal="true"][aria-hidden="false"]'
        )) {
            return false;
        }
        if (step.requiresEnabled && target.disabled) return false;
        const style = window.getComputedStyle(target);
        const rect = target.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
    }

    function setDialogInterruption(step, interrupted) {
        actions.querySelectorAll('button').forEach(button => {
            if (button.disabled !== interrupted) button.disabled = interrupted;
        });
        const message = interrupted
            ? '열린 창에서 작업을 마친 뒤 창을 닫아주세요'
            : (currentReminder() || step.hint || '강조된 실제 화면을 눌러주세요');
        const hidden = !(step.action || interrupted);
        if (hint.textContent !== message) hint.textContent = message;
        if (hint.hidden !== hidden) hint.hidden = hidden;
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

    async function prepareInvoice() {
        const createTab = document.getElementById('create-tab');
        if (!createTab) return;
        if (typeof window.showTab === 'function') window.showTab('create-tab');

        const setChecked = (selector, checked) => {
            document.querySelectorAll(selector).forEach(checkbox => {
                if (checkbox.checked === checked) return;
                checkbox.checked = checked;
                checkbox.dispatchEvent(new Event('change', {bubbles: true}));
            });
        };
        const tutorialWater = document.querySelector('.water-item[data-month^="2026-10"]');
        if (tutorialWater) {
            tutorialWater.dataset.desc = '2026년 9~10월 사용분 수도요금 (10월 고지)';
            document.querySelectorAll('[data-tutorial-water-period]').forEach(element => {
                element.hidden = false;
            });
        }

        if (createTab.dataset.tutorialPrepared !== 'true') {
            const name = document.getElementById('invoiceName');
            const memo = document.getElementById('invoiceMemo');
            if (name && !name.value) {
                name.value = '2026년 10월 정산 (연습)';
                name.dispatchEvent(new Event('input', {bubbles: true}));
            }
            if (memo && !memo.value) {
                memo.value = '모든 층의 9·10월 전기요금과 9~10월 사용분 수도요금을 묶는 연습용 정산서입니다.';
            }
            setChecked('.electric-item, .water-item, .common-item', false);
            createTab.dataset.tutorialPrepared = 'true';
        }

        const currentStep = readStep();
        if (currentStep >= 12) setChecked('.electric-item[data-month^="2026-09"]', true);
        if (currentStep >= 13) setChecked('.electric-item[data-month^="2026-10"]', true);
        if (currentStep >= 14) setChecked('.water-item[data-month^="2026-10"]', true);
        const step2 = document.getElementById('step2');
        if (currentStep >= 15 && step2?.hidden && typeof window.goToStep2 === 'function') {
            await window.goToStep2();
        }
        const step3 = document.getElementById('step3');
        if (currentStep >= 16 && step3?.hidden && typeof window.goToStep3 === 'function') {
            await window.goToStep3();
        }
    }

    function expectedEndpoint(step) {
        return config.endpoint === step.endpoint;
    }

    function endpointPath(step) {
        const paths = {
            index: '/', settings: '/settings', calculator: '/calculator',
            view_bills: '/view', invoice_combination: '/invoice', payments: '/payments',
            view_invoice: config.latestInvoiceUrl || '/invoice?tab=history',
            print_invoice: config.latestInvoicePrintUrl || '/invoice?tab=history'
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
                clearTutorialStorage();
                window.location.assign(controlUrl('complete', 'setup'));
            });
            actions.appendChild(complete);
            if (!config.hadUserData) {
                const importButton = document.createElement('button');
                importButton.className = 'btn btn-secondary';
                importButton.textContent = '완료하고 백업 파일 가져오기';
                importButton.addEventListener('click', () => {
                    clearTutorialStorage();
                    window.location.assign(controlUrl('complete', 'import'));
                });
                actions.appendChild(importButton);
            }
            return;
        }
        if (!step.action) {
            const next = document.createElement('button');
            next.className = 'btn btn-primary';
            next.textContent = step.nextLabel || '다음 단계';
            next.addEventListener('click', () => advance(step, index));
            actions.appendChild(next);
        }
        if (step.action && step.skipActionLabel) {
            const skip = document.createElement('button');
            skip.className = 'btn btn-secondary';
            skip.textContent = step.skipActionLabel;
            skip.addEventListener('click', () => advance(step, index));
            actions.appendChild(skip);
        }
    }

    function bindTarget(target, step, index) {
        if (targetCleanup) targetCleanup();
        targetCleanup = null;
        if (!step.action) return;

        if (step.completionEvent) {
            const completed = event => {
                if (step.completionCondition && !step.completionCondition(target, event)) return;
                window.removeEventListener(step.completionEvent, completed);
                targetCleanup = null;
                writeStep(index + 1);
                window.setTimeout(() => scheduleRender(80), 80);
            };
            window.addEventListener(step.completionEvent, completed);
            targetCleanup = () => window.removeEventListener(step.completionEvent, completed);
            return;
        }

        const interact = event => {
            if (step.completionCondition && !step.completionCondition(target, event)) return;
            writeStep(index + 1);
            const anchor = target.matches('a[href]') ? target : target.closest('a[href]');
            const href = anchor?.getAttribute('href');
            if (step.overridePath || (event.type === 'click' && href && href !== '#')) {
                event.preventDefault();
                const destination = new URL(step.overridePath || anchor.href, window.location.href);
                destination.searchParams.set(navigationStepParameter, String(index + 1));
                window.location.assign(destination.toString());
                return;
            }
            window.setTimeout(() => scheduleRender(80), 80);
        };
        const eventName = step.interactionEvent || 'click';
        target.addEventListener(eventName, interact, {capture: true, once: true});
        targetCleanup = () => target.removeEventListener(eventName, interact, {capture: true});
    }

    function currentReminder() {
        return Date.now() < reminderUntil ? reminderMessage : '';
    }

    function remindCurrentTarget() {
        reminderMessage = '현재 안내된 작업을 먼저 완료하세요.';
        reminderUntil = Date.now() + 1800;
        hint.textContent = reminderMessage;
        hint.hidden = false;
        focusRing.classList.remove('is-reminded');
        cue.classList.remove('is-reminded');
        void focusRing.offsetWidth;
        focusRing.classList.add('is-reminded');
        cue.classList.add('is-reminded');
        window.setTimeout(() => {
            focusRing.classList.remove('is-reminded');
            cue.classList.remove('is-reminded');
        }, 700);
    }

    function interactionAllowed(origin) {
        if (isPracticeMode()) return true;
        if (!(origin instanceof Element)) return true;
        if (origin.closest('#tutorialGuide')) return true;
        const dialog = activeDialog();
        if (dialog?.contains(origin)) return true;
        if (!guardedTarget || !guardedStep || readStep() !== guardedIndex) return false;
        if (guardedStep.action) return guardedTarget === origin || guardedTarget.contains(origin);
        const editable = origin.closest('input, select, textarea, [contenteditable="true"]');
        if (editable && guardedTarget.contains(editable)) return true;
        const label = origin.closest('label');
        return Boolean(label && guardedTarget.contains(label) && label.control && guardedTarget.contains(label.control));
    }

    function guardClick(event) {
        if (!event.isTrusted) return;
        const origin = event.target;
        if (interactionAllowed(origin)) return;
        if (!(origin instanceof Element) || !origin.closest(
            'a, button, input, select, textarea, label, summary, [role="button"], [onclick], [tabindex="0"]'
        )) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        remindCurrentTarget();
    }

    function guardSubmit(event) {
        if (!event.isTrusted) return;
        if (isPracticeMode()) return;
        const dialog = activeDialog();
        if (dialog?.contains(event.target)) return;
        const submitter = event.submitter;
        if (guardedStep?.action && guardedTarget && submitter
            && (guardedTarget === submitter || guardedTarget.contains(submitter))) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        remindCurrentTarget();
    }

    function guardChange(event) {
        if (!event.isTrusted) return;
        if (interactionAllowed(event.target)) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        remindCurrentTarget();
    }

    function guardKeydown(event) {
        if (!event.isTrusted) return;
        if (!['Enter', ' '].includes(event.key) || interactionAllowed(event.target)) return;
        const origin = event.target;
        if (!(origin instanceof Element) || !origin.closest(
            'a, button, input, select, textarea, summary, [role="button"], [onclick], [tabindex="0"]'
        )) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        remindCurrentTarget();
    }

    function stopTracking() {
        if (trackingCleanup) trackingCleanup();
        trackingCleanup = null;
    }

    function startTargetTracking(target, step, index) {
        stopTracking();
        let positionFrame = null;
        let resizeObserver = null;
        let mutationObserver = null;

        const position = () => {
            positionFrame = null;
            if (readStep() !== index || !target.isConnected) {
                scheduleRender(20);
                return;
            }
            const dialog = syncDialogState();
            const interrupted = Boolean(dialog && !dialog.contains(target));
            const visibleTarget = interrupted
                ? (dialog.querySelector('.modal-content') || dialog)
                : target;
            setDialogInterruption(step, interrupted);
            const message = interrupted
                ? '열린 창에서 작업을 마친 뒤 닫아주세요'
                : (currentReminder() || step.cue);
            focusTarget(
                targetIsVisible(visibleTarget, interrupted ? {} : step) ? visibleTarget : null,
                message
            );
        };
        const schedulePosition = () => {
            if (positionFrame === null) positionFrame = window.requestAnimationFrame(position);
        };
        const options = {passive: true, capture: true};
        document.addEventListener('scroll', schedulePosition, options);
        window.addEventListener('resize', schedulePosition, options);
        window.addEventListener('orientationchange', schedulePosition, options);
        window.addEventListener('focus', schedulePosition, options);
        document.addEventListener('visibilitychange', schedulePosition, options);
        if (window.visualViewport) {
            window.visualViewport.addEventListener('resize', schedulePosition, options);
            window.visualViewport.addEventListener('scroll', schedulePosition, options);
        }
        if ('ResizeObserver' in window) {
            resizeObserver = new ResizeObserver(schedulePosition);
            resizeObserver.observe(target);
            resizeObserver.observe(guide);
            const dialog = activeDialog();
            if (dialog) resizeObserver.observe(dialog);
        }
        if ('MutationObserver' in window) {
            mutationObserver = new MutationObserver(schedulePosition);
            mutationObserver.observe(document.body, {
                childList: true, subtree: true, attributes: true,
                attributeFilter: ['class', 'hidden', 'open', 'aria-modal', 'aria-hidden']
            });
        }
        const interval = window.setInterval(schedulePosition, 250);
        trackingCleanup = () => {
            if (positionFrame !== null) window.cancelAnimationFrame(positionFrame);
            document.removeEventListener('scroll', schedulePosition, options);
            window.removeEventListener('resize', schedulePosition, options);
            window.removeEventListener('orientationchange', schedulePosition, options);
            window.removeEventListener('focus', schedulePosition, options);
            document.removeEventListener('visibilitychange', schedulePosition, options);
            if (window.visualViewport) {
                window.visualViewport.removeEventListener('resize', schedulePosition, options);
                window.visualViewport.removeEventListener('scroll', schedulePosition, options);
            }
            resizeObserver?.disconnect();
            mutationObserver?.disconnect();
            window.clearInterval(interval);
        };
        schedulePosition();
    }

    function scrollTargetIntoUsableArea(target) {
        target.scrollIntoView({behavior: 'auto', block: 'center', inline: 'nearest'});
        window.requestAnimationFrame(() => {
            const rect = target.getBoundingClientRect();
            const bounds = usableBounds();
            const margin = 16;
            let delta = 0;
            if (rect.bottom > bounds.bottom - margin) delta = rect.bottom - bounds.bottom + margin;
            if (rect.top < bounds.top + margin) delta = rect.top - bounds.top - margin;
            if (delta) window.scrollBy({top: delta, behavior: 'auto'});
        });
    }

    async function render() {
        window.clearTimeout(renderTimer);
        stopTracking();
        if (targetCleanup) targetCleanup();
        targetCleanup = null;
        guardedTarget = null;
        guardedStep = null;
        guardedIndex = null;

        if (isPracticeMode()) {
            document.body.classList.add('tutorial-practice-mode');
            document.body.classList.remove('tutorial-dialog-open');
            guide.hidden = true;
            practiceDock.hidden = false;
            hideFocus();
            return;
        }

        document.body.classList.remove('tutorial-practice-mode');
        guide.hidden = false;
        practiceDock.hidden = true;

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
        hint.textContent = step.hint || '강조된 실제 화면을 눌러주세요';
        hint.hidden = !step.action;
        renderActions(step, index);
        setDialogInterruption(step, false);

        const target = document.querySelector(step.selector);
        if (step.final && !targetIsVisible(target, step)) {
            missingModalAttempts = 0;
            syncDialogState();
            hideFocus();
            return;
        }
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
            syncDialogState();
            hideFocus();
            scheduleRender(140);
            return;
        }

        missingModalAttempts = 0;
        guardedTarget = target;
        guardedStep = step;
        guardedIndex = index;
        openNavigationFor(target);
        scrollTargetIntoUsableArea(target);
        bindTarget(target, step, index);
        startTargetTracking(target, step, index);
    }

    function scheduleRender(delay = 80) {
        window.clearTimeout(renderTimer);
        renderTimer = window.setTimeout(render, delay);
    }

    function exitTutorial() {
        if (!window.confirm('연습을 종료하면 입력 내용과 진행 상태가 삭제됩니다. 종료할까요?')) return;
        clearTutorialStorage();
        window.location.assign(controlUrl('exit'));
    }

    function restartTutorial() {
        if (!window.confirm('연습 내용을 지우고 첫 단계부터 다시 시작할까요?')) return;
        clearTutorialStorage();
        window.location.assign(controlUrl('restart'));
    }

    document.getElementById('tutorialExitClose').addEventListener('click', exitTutorial);
    document.getElementById('tutorialExit').addEventListener('click', exitTutorial);
    document.getElementById('tutorialPracticeStart').addEventListener('click', enterPracticeMode);
    document.getElementById('tutorialRestart').addEventListener('click', restartTutorial);
    document.getElementById('tutorialPracticeRestart').addEventListener('click', restartTutorial);
    document.getElementById('tutorialPracticeFinish').addEventListener('click', exitTutorial);
    document.addEventListener('keydown', event => {
        if (event.key === 'Escape' && config.endpoint !== 'print_invoice' && !isPracticeMode()) exitTutorial();
    });
    document.addEventListener('click', guardClick, true);
    document.addEventListener('change', guardChange, true);
    document.addEventListener('keydown', guardKeydown, true);
    document.addEventListener('submit', guardSubmit, true);

    render();
})();
