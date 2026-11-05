"""요금 배분의 순수 계산 엔진.

미리보기와 실제 저장이 같은 함수를 사용하도록 DB 쓰기와 계산을 분리한다.
모든 분배는 1전(0.01원) 단위까지 합계가 정확히 맞도록 나머지를 안정적인
세대 ID 순서로 배분한다. 최종 청구액만 기존 정책대로 10원 단위 올림한다.
"""

from decimal import Decimal, ROUND_DOWN


CENT = Decimal("0.01")


class AllocationError(ValueError):
    """입력값으로 유효한 배분을 만들 수 없을 때 발생한다."""


def as_decimal(value):
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value or 0))


def round_up_to_10(amount):
    value = as_decimal(amount)
    if value <= 0:
        return 0
    return int(((value + Decimal("9.999999999")) // 10) * 10)


def split_exact(total, weighted_ids):
    """``total``을 ``[(id, weight)]``에 비례해 정확히 나눈다.

    결과 합계는 소수 둘째 자리에서 항상 입력 합계와 일치한다. 동일한 잔여
    소수점이면 작은 세대 ID가 먼저 0.01원을 받으므로 실행마다 결과가 같다.
    """
    total = as_decimal(total).quantize(CENT)
    entries = [(identifier, as_decimal(weight)) for identifier, weight in weighted_ids]
    if not entries:
        if total != 0:
            raise AllocationError("배분할 세대가 없습니다.")
        return {}
    if total < 0:
        raise AllocationError("배분 금액은 음수일 수 없습니다.")

    weight_total = sum((weight for _, weight in entries), Decimal(0))
    if weight_total <= 0:
        entries = [(identifier, Decimal(1)) for identifier, _ in entries]
        weight_total = Decimal(len(entries))

    raw = {
        identifier: total * weight / weight_total
        for identifier, weight in entries
    }
    result = {
        identifier: value.quantize(CENT, rounding=ROUND_DOWN)
        for identifier, value in raw.items()
    }
    remaining_cents = int(
        ((total - sum(result.values(), Decimal(0))) / CENT).to_integral_value()
    )
    order = sorted(
        entries,
        key=lambda entry: (-(raw[entry[0]] - result[entry[0]]), entry[0]),
    )
    for index in range(remaining_cents):
        result[order[index % len(order)][0]] += CENT
    return result


def _discount_map(total_input, units, flag_name, configured_per_unit, month_count=1):
    """할인 수혜 세대와 적용 방식을 결정한다.

    입력 할인액이 있으면 특정 대상이 존재할 때는 대상에게, 대상이 하나도
    없을 때는 정산 대상 전체에게 균등 배분한다. 입력액이 0이면 기존 정책대로
    설정 단가를 특정 대상 세대에만 적용한다.
    """
    total_input = as_decimal(total_input)
    target_units = [unit for unit in units if bool(getattr(unit, flag_name))]
    if total_input > 0:
        recipients = target_units or list(units)
        mode = "targeted" if target_units else "floor_equal"
        return split_exact(total_input, [(unit.id, 1) for unit in recipients]), mode
    if not target_units:
        return {unit.id: Decimal(0) for unit in units}, "none"

    per_unit = as_decimal(configured_per_unit) * month_count
    return {unit.id: per_unit for unit in target_units}, "configured"


def allocate_electric(
    units,
    readings,
    *,
    total_amount,
    welfare_discount_input,
    voucher_discount_input,
    tv_fee_total,
    tv_distribution_mode,
    month_count,
    configured_tv_fee,
    configured_welfare_discount,
    configured_voucher_discount,
):
    if not units:
        raise AllocationError("전기요금을 배분할 재실 세대가 없습니다.")

    usage = {}
    for unit in units:
        previous, current = readings[unit.id]
        previous = as_decimal(previous)
        current = as_decimal(current)
        if current < previous:
            raise AllocationError(
                "{}의 현재 검침값이 이전 검침값보다 작습니다.".format(unit.unit_name)
            )
        usage[unit.id] = current - previous

    welfare, welfare_mode = _discount_map(
        welfare_discount_input,
        units,
        "electric_welfare",
        configured_welfare_discount,
        month_count,
    )
    voucher, voucher_mode = _discount_map(
        voucher_discount_input,
        units,
        "electric_voucher",
        configured_voucher_discount,
        month_count,
    )
    welfare_total = sum(welfare.values(), Decimal(0))
    voucher_total = sum(voucher.values(), Decimal(0))
    gross_amount = as_decimal(total_amount) + welfare_total + voucher_total

    total_usage = sum(usage.values(), Decimal(0))
    zero_usage_fallback = total_usage <= 0
    weights = [(unit.id, usage[unit.id] if total_usage > 0 else 1) for unit in units]
    base = split_exact(gross_amount, weights)

    if tv_distribution_mode == "EQUAL":
        tv = split_exact(tv_fee_total, [(unit.id, 1) for unit in units])
    else:
        tv_per_unit = as_decimal(configured_tv_fee) * month_count
        tv = {
            unit.id: tv_per_unit if bool(unit.has_tv) else Decimal(0)
            for unit in units
        }

    details = []
    for unit in sorted(units, key=lambda row: row.id):
        final_amount = base[unit.id] - welfare.get(unit.id, 0) - voucher.get(unit.id, 0) + tv[unit.id]
        if final_amount < 0:
            raise AllocationError(
                "{}의 할인액이 배분 가능한 요금을 초과합니다.".format(unit.unit_name)
            )
        details.append(
            {
                "unit": unit,
                "usage_amount": usage[unit.id],
                "base_amount": base[unit.id],
                "welfare_discount": welfare.get(unit.id, Decimal(0)),
                "voucher_discount": voucher.get(unit.id, Decimal(0)),
                "tv_fee": tv[unit.id],
                "final_amount": final_amount,
                "charged_amount": round_up_to_10(final_amount),
            }
        )

    warnings = []
    if welfare_mode == "floor_equal":
        warnings.append("특정 복지할인 대상이 없어 정산 대상 세대 전체에 균등 배분했습니다.")
    if voucher_mode == "floor_equal":
        warnings.append("특정 바우처 대상이 없어 정산 대상 세대 전체에 균등 배분했습니다.")
    if zero_usage_fallback:
        warnings.append("전체 사용량이 0이므로 정산 대상 세대에 균등 배분했습니다.")
    return _result(details, warnings, {
        "welfare_discount_total": welfare_total,
        "voucher_discount_total": voucher_total,
        "welfare_distribution": welfare_mode,
        "voucher_distribution": voucher_mode,
        "zero_usage_fallback": zero_usage_fallback,
    })


def allocate_water(
    all_units,
    included_units,
    *,
    total_amount,
    welfare_discount_input,
    configured_welfare_discount,
):
    if not all_units:
        raise AllocationError("수도요금을 배분할 재실 세대가 없습니다.")
    if not included_units and as_decimal(total_amount) > 0:
        raise AllocationError("모든 세대를 제외하면 수도요금을 배분할 수 없습니다.")

    welfare, welfare_mode = _discount_map(
        welfare_discount_input,
        included_units,
        "water_welfare",
        configured_welfare_discount,
    )
    welfare_total = sum(welfare.values(), Decimal(0))
    gross_amount = as_decimal(total_amount) + welfare_total
    total_residents = sum((unit.residents_count for unit in included_units), 0)
    zero_residents_fallback = bool(included_units) and total_residents <= 0
    weights = [
        (unit.id, unit.residents_count if total_residents > 0 else 1)
        for unit in included_units
    ]
    base = split_exact(gross_amount, weights)
    included_ids = {unit.id for unit in included_units}

    details = []
    for unit in sorted(all_units, key=lambda row: row.id):
        if unit.id not in included_ids:
            details.append({
                "unit": unit,
                "base_amount": Decimal(0),
                "welfare_discount": Decimal(0),
                "final_amount": Decimal(0),
                "charged_amount": 0,
                "is_excluded": True,
            })
            continue
        final_amount = base[unit.id] - welfare.get(unit.id, 0)
        if final_amount < 0:
            raise AllocationError(
                "{}의 할인액이 배분 가능한 요금을 초과합니다.".format(unit.unit_name)
            )
        details.append({
            "unit": unit,
            "base_amount": base[unit.id],
            "welfare_discount": welfare.get(unit.id, Decimal(0)),
            "final_amount": final_amount,
            "charged_amount": round_up_to_10(final_amount),
            "is_excluded": False,
        })

    warnings = []
    if welfare_mode == "floor_equal":
        warnings.append("특정 수도 복지할인 대상이 없어 정산 대상 세대 전체에 균등 배분했습니다.")
    if zero_residents_fallback:
        warnings.append("정산 대상의 거주 인원이 모두 0명이므로 세대 균등으로 배분했습니다.")
    return _result(details, warnings, {
        "welfare_discount_total": welfare_total,
        "welfare_distribution": welfare_mode,
        "zero_residents_fallback": zero_residents_fallback,
    })


def allocate_common(units, *, total_amount, distribution_method):
    if not units:
        raise AllocationError("배분할 재실 세대가 없습니다.")
    total_residents = sum((unit.residents_count for unit in units), 0)
    fallback = distribution_method == "BY_RESIDENTS" and total_residents <= 0
    weights = [
        (unit.id, unit.residents_count if distribution_method == "BY_RESIDENTS" and total_residents > 0 else 1)
        for unit in units
    ]
    amounts = split_exact(total_amount, weights)
    details = [
        {
            "unit": unit,
            "amount": amounts[unit.id],
            "final_amount": amounts[unit.id],
            "charged_amount": round_up_to_10(amounts[unit.id]),
        }
        for unit in sorted(units, key=lambda row: row.id)
    ]
    warnings = ["거주 인원이 모두 0명이므로 세대 균등으로 배분했습니다."] if fallback else []
    return _result(details, warnings, {"zero_residents_fallback": fallback})


def _result(details, warnings, extra):
    final_total = sum((row["final_amount"] for row in details), Decimal(0))
    charged_total = sum((row["charged_amount"] for row in details), 0)
    result = {
        "details": details,
        "warnings": warnings,
        "final_total": final_total,
        "charged_total": charged_total,
        "rounding_difference": as_decimal(charged_total) - final_total,
    }
    result.update(extra)
    return result
