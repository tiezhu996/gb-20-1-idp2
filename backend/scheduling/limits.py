"""每日上课上限校验：教师每天最多节数、同一门课在一个班每天最多节数。

自动排课在 CSP 求解阶段遵守这两条约束；调课、代课在落库前再核一遍，
越界则整笔操作不动，并返回具体是哪门课、星期几、第几节越界。
"""
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from core.models import Course, Teacher
from .models import ScheduleEntry


WEEKDAY_CN = {1: '一', 2: '二', 3: '三', 4: '四', 5: '五',
              6: '六', 7: '日'}


def weekday_cn(day: int) -> str:
    return f"周{WEEKDAY_CN.get(day, day)}"


def _load_limits() -> Tuple[Dict[int, Optional[int]], Dict[int, Optional[int]]]:
    teacher_limits = dict(Teacher.objects.values_list('id', 'max_daily_lessons'))
    course_limits = dict(Course.objects.values_list('id', 'max_daily_per_class'))
    return teacher_limits, course_limits


def _build_counts(semester_id: int, exclude_ids: List[int]):
    """统计排除指定条目后，教师/课程-班级每天已排节数。"""
    teacher_counts: Dict[Tuple[int, int], int] = defaultdict(int)
    course_counts: Dict[Tuple[int, int, int], int] = defaultdict(int)

    entries = ScheduleEntry.objects.filter(
        semester_id=semester_id
    ).exclude(
        id__in=exclude_ids
    ).values_list('teacher_id', 'class_id', 'course_id', 'day_of_week')

    for teacher_id, class_id, course_id, day in entries:
        teacher_counts[(teacher_id, day)] += 1
        course_counts[(class_id, course_id, day)] += 1

    return teacher_counts, course_counts


def _check_proposed(
    semester_id: int,
    proposed: List[Dict]
) -> List[str]:
    """校验一批拟变更条目是否会突破每日上限。

    proposed 每项: {course_name, teacher_name, class_name, day, period,
                    teacher_id, class_id, course_id}
    """
    teacher_limits, course_limits = _load_limits()
    # 同一批变更先在内存里模拟，再统一计数
    exclude_ids = [p['entry_id'] for p in proposed]
    teacher_counts, course_counts = _build_counts(semester_id, exclude_ids)

    # 预载名称，保证提示信息可读
    teacher_names = dict(Teacher.objects.values_list('id', 'name'))
    course_names = dict(Course.objects.values_list('id', 'name'))

    violations: List[str] = []

    for p in proposed:
        day = p['day']
        period = p['period']
        where = f"{weekday_cn(day)}第{period}节"
        cname = p.get('course_name') or course_names.get(p['course_id'], p['course_id'])
        tname = p.get('teacher_name') or teacher_names.get(p['teacher_id'], p['teacher_id'])

        teacher_counts[(p['teacher_id'], day)] += 1
        teacher_total = teacher_counts[(p['teacher_id'], day)]
        t_limit = teacher_limits.get(p['teacher_id'])
        if t_limit is not None and teacher_total > t_limit:
            violations.append(
                f"{cname}（{where}）：教师 {tname} {weekday_cn(day)}当天共 {teacher_total} 节，"
                f"超过每天最多 {t_limit} 节的上限"
            )

        course_counts[(p['class_id'], p['course_id'], day)] += 1
        course_total = course_counts[(p['class_id'], p['course_id'], day)]
        c_limit = course_limits.get(p['course_id'])
        if c_limit is not None and course_total > c_limit:
            violations.append(
                f"{cname}（{where}）：{p.get('class_name', '')} {weekday_cn(day)}当天该课共 {course_total} 节，"
                f"超过同一门课每班每天最多 {c_limit} 节的上限"
            )

    return violations


def check_swap(entry1: ScheduleEntry, entry2: ScheduleEntry) -> List[str]:
    """调课：两节课交换时间（教师、课程随课走），核对上限。"""
    proposed = [
        {
            'entry_id': entry1.id,
            'course_name': entry1.course.name,
            'teacher_name': entry1.teacher.name,
            'class_name': entry1.class_id.name,
            'day': entry2.day_of_week,
            'period': entry2.period,
            'teacher_id': entry1.teacher_id,
            'class_id': entry1.class_id_id,
            'course_id': entry1.course_id,
        },
        {
            'entry_id': entry2.id,
            'course_name': entry2.course.name,
            'teacher_name': entry2.teacher.name,
            'class_name': entry2.class_id.name,
            'day': entry1.day_of_week,
            'period': entry1.period,
            'teacher_id': entry2.teacher_id,
            'class_id': entry2.class_id_id,
            'course_id': entry2.course_id,
        },
    ]
    return _check_proposed(entry1.semester_id, proposed)


def check_substitute(entry: ScheduleEntry, substitute_teacher: Teacher) -> List[str]:
    """代课：时间地点不变，仅换教师，核对新教师当天上限。"""
    proposed = [
        {
            'entry_id': entry.id,
            'course_name': entry.course.name,
            'teacher_name': substitute_teacher.name,
            'class_name': entry.class_id.name,
            'day': entry.day_of_week,
            'period': entry.period,
            'teacher_id': substitute_teacher.id,
            'class_id': entry.class_id_id,
            'course_id': entry.course_id,
        }
    ]
    return _check_proposed(entry.semester_id, proposed)
