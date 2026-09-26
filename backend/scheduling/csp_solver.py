import random
from typing import List, Dict, Tuple, Set, Optional
from dataclasses import dataclass
from collections import defaultdict


@dataclass
class TimeSlot:
    day: int
    period: int

    def __hash__(self):
        return hash((self.day, self.period))

    def __eq__(self, other):
        return self.day == other.day and self.period == other.period


@dataclass
class SchedulingTask:
    class_id: int
    course_id: int
    teacher_id: int
    weekly_hours: int
    preferred_room_type: str
    priority: str
    available_time_slots: List[TimeSlot]
    classroom_capacity: int
    teacher_max_daily: Optional[int] = None
    course_max_daily: Optional[int] = None
    class_name: str = ''
    course_name: str = ''


class CSPScheduler:
    def __init__(self, semester):
        self.semester = semester
        self.weekly_days = semester.weekly_days
        self.daily_periods = len(semester.daily_periods) if semester.daily_periods else 7
        self.all_slots = [
            TimeSlot(day=d + 1, period=p + 1)
            for d in range(self.weekly_days)
            for p in range(self.daily_periods)
        ]
        self.classroom_usage = defaultdict(set)
        self.teacher_usage = defaultdict(set)
        self.class_usage = defaultdict(set)
        # 教师每天已排节数: teacher_id -> {day: count}
        self.teacher_daily = defaultdict(lambda: defaultdict(int))
        # 同一门课在一个班每天已排节数: (class_id, course_id) -> {day: count}
        self.class_course_daily = defaultdict(lambda: defaultdict(int))
        self.assignments = []
        self.conflicts = []

    def generate_time_slots_for_priority(self, priority: str) -> List[TimeSlot]:
        """按优先级返回候选时段（组内随机打散，避免所有课都堆到周一第一节）。"""
        morning_periods = min(4, self.daily_periods)
        morning_slots = [s for s in self.all_slots if s.period <= morning_periods]
        afternoon_slots = [s for s in self.all_slots if s.period > morning_periods]
        random.shuffle(morning_slots)
        random.shuffle(afternoon_slots)

        if priority == 'high':
            return morning_slots + afternoon_slots
        elif priority == 'low':
            return afternoon_slots + morning_slots
        else:
            slots = list(self.all_slots)
            random.shuffle(slots)
            return slots

    def is_available(
        self,
        time_slot: TimeSlot,
        teacher_id: int,
        class_id: int,
        classroom_id: int,
        teacher_available_slots: Set[TimeSlot]
    ) -> bool:
        if teacher_available_slots and time_slot not in teacher_available_slots:
            return False
        if time_slot in self.teacher_usage[teacher_id]:
            return False
        if time_slot in self.class_usage[class_id]:
            return False
        if time_slot in self.classroom_usage[classroom_id]:
            return False
        return True

    def within_daily_limits(
        self,
        time_slot: TimeSlot,
        task: SchedulingTask
    ) -> bool:
        """检查教师每天最多节数、同一门课在一个班每天最多节数。"""
        if task.teacher_max_daily is not None:
            if self.teacher_daily[task.teacher_id][time_slot.day] >= task.teacher_max_daily:
                return False
        if task.course_max_daily is not None:
            if self.class_course_daily[(task.class_id, task.course_id)][time_slot.day] \
                    >= task.course_max_daily:
                return False
        return True

    def get_compatible_classrooms(
        self,
        preferred_room_type: str,
        required_capacity: int,
        classrooms_data: Dict[int, Dict]
    ) -> List[int]:
        compatible = []
        for cid, cdata in classrooms_data.items():
            if cdata['room_type'] == preferred_room_type and cdata['capacity'] >= required_capacity:
                compatible.append(cid)
        if not compatible:
            for cid, cdata in classrooms_data.items():
                if cdata['capacity'] >= required_capacity:
                    compatible.append(cid)
        return compatible

    def _record_assignment(
        self,
        slot: TimeSlot,
        class_id: int,
        course_id: int,
        teacher_id: int,
        classroom_id: int
    ):
        self.classroom_usage[classroom_id].add(slot)
        self.teacher_usage[teacher_id].add(slot)
        self.class_usage[class_id].add(slot)
        self.teacher_daily[teacher_id][slot.day] += 1
        self.class_course_daily[(class_id, course_id)][slot.day] += 1

    def schedule(
        self,
        tasks: List[SchedulingTask],
        classrooms_data: Dict[int, Dict],
        teachers_data: Dict[int, Dict],
        locked_entries: Optional[List[Dict]] = None
    ) -> Tuple[List[Dict], List[Dict]]:
        self.assignments = []
        self.conflicts = []
        self.classroom_usage.clear()
        self.teacher_usage.clear()
        self.class_usage.clear()
        self.teacher_daily.clear()
        self.class_course_daily.clear()

        # 锁定的课留在原处，并计入占用与每日数量
        if locked_entries:
            for entry in locked_entries:
                slot = TimeSlot(day=entry['day_of_week'], period=entry['period'])
                self._record_assignment(
                    slot,
                    entry['class_id'],
                    entry.get('course_id'),
                    entry['teacher_id'],
                    entry['classroom_id']
                )
                self.assignments.append(entry)

        priority_order = {'high': 0, 'medium': 1, 'low': 2}
        sorted_tasks = sorted(
            tasks,
            key=lambda t: (priority_order[t.priority], -t.weekly_hours)
        )

        for task in sorted_tasks:
            teacher_available = set()
            if teachers_data.get(task.teacher_id, {}).get('available_time_slots'):
                for slot_dict in teachers_data[task.teacher_id]['available_time_slots']:
                    teacher_available.add(
                        TimeSlot(day=slot_dict['day'], period=slot_dict['period'])
                    )

            compatible_rooms = self.get_compatible_classrooms(
                task.preferred_room_type,
                task.classroom_capacity,
                classrooms_data
            )

            if not compatible_rooms:
                self.conflicts.append({
                    'type': 'classroom',
                    'task': f"{task.class_name or ('班级' + str(task.class_id))}"
                            f"的{task.course_name or ('课程' + str(task.course_id))}",
                    'message': f"没有找到适合 {task.preferred_room_type} 类型的教室"
                })
                continue

            candidate_slots = self.generate_time_slots_for_priority(task.priority)
            hours_assigned = 0

            # 每个候选时段最多尝试一次，符合全部硬约束与每日上限才排，排不下就留空
            for slot in candidate_slots:
                if hours_assigned >= task.weekly_hours:
                    break
                if not self.within_daily_limits(slot, task):
                    continue

                available_room = None
                for room in compatible_rooms:
                    if self.is_available(
                        slot,
                        task.teacher_id,
                        task.class_id,
                        room,
                        teacher_available
                    ):
                        available_room = room
                        break

                if available_room:
                    self._record_assignment(
                        slot,
                        task.class_id,
                        task.course_id,
                        task.teacher_id,
                        available_room
                    )
                    self.assignments.append({
                        'semester_id': self.semester.id,
                        'class_id': task.class_id,
                        'course_id': task.course_id,
                        'teacher_id': task.teacher_id,
                        'classroom_id': available_room,
                        'day_of_week': slot.day,
                        'period': slot.period,
                        'is_locked': False
                    })
                    hours_assigned += 1

            if hours_assigned < task.weekly_hours:
                self.conflicts.append({
                    'type': 'insufficient_slots',
                    'task': f"{task.class_name or ('班级' + str(task.class_id))}"
                            f"的{task.course_name or ('课程' + str(task.course_id))}",
                    'message': (
                        f"{task.class_name or ('班级' + str(task.class_id))} 的 "
                        f"{task.course_name or ('课程' + str(task.course_id))} "
                        f"仅安排了 {hours_assigned}/{task.weekly_hours} 课时，"
                        f"剩余 {task.weekly_hours - hours_assigned} 课时受时间冲突或每日上限限制无法排入"
                    )
                })

        return self.assignments, self.conflicts


# ---------------------------------------------------------------------------
# 调课/代课上限校验
# ---------------------------------------------------------------------------

WEEKDAY_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def weekday_name(day_of_week: int) -> str:
    if 1 <= day_of_week <= 7:
        return WEEKDAY_NAMES[day_of_week - 1]
    return f'周{day_of_week}'


def validate_cap_change(entries: List[Dict], changes: Dict[int, Tuple[int, int, Optional[int]]],
                        teacher_limits_override: Optional[Dict[int, Optional[int]]] = None):
    """在内存中模拟移动/换老师后校验每日上限及基础时间冲突。

    :param entries: 学期内全部课表条目（dict，需含 id/class_id/course/teacher/
                    classroom/day_of_week/period，可带 *_max_daily 与 *_name 字段）
    :param changes: {entry_id: (new_day, new_period, new_teacher_id_or_None)}
    :param teacher_limits_override: 教师每日上限映射 {teacher_id: limit or None}，
                    代课换老师后按教师表当前上限校验，避免沿用条目上的旧值
    :return: (violations, changed_entry_ids)
             violations 为结构化违例列表，元素形如：
             ('teacher_cap', teacher_id, day, count, limit)
             ('course_cap', class_id, course_id, day, count, limit)
             ('time_conflict', kind, day, period, count)
             kind 为 teacher/classroom/class
    """
    violations = []
    changed_ids = [eid for eid in changes if any(e['id'] == eid for e in entries)]

    def changed_day(e):
        ch = changes.get(e['id'])
        return ch[0] if ch else e['day_of_week']

    def changed_period(e):
        ch = changes.get(e['id'])
        return ch[1] if ch else e['period']

    def changed_teacher(e):
        ch = changes.get(e['id'])
        return ch[2] if ch and ch[2] is not None else e['teacher_id']

    # 受影响的教师日 / 班级-课程日 / 时间槽
    affected_teacher_days = set()
    affected_class_course_days = set()
    affected_slots = set()
    for e in entries:
        if e['id'] in changes:
            new_day, new_period, _ = changes[e['id']]
            affected_teacher_days.add((changed_teacher(e), new_day))
            affected_teacher_days.add((e['teacher_id'], e['day_of_week']))
            affected_class_course_days.add((e['class_id'], e['course'], new_day))
            affected_class_course_days.add(
                (e['class_id'], e['course'], e['day_of_week'])
            )
            affected_slots.add((new_day, new_period))
            affected_slots.add((e['day_of_week'], e['period']))

    # 教师每天节数上限
    teacher_daily = defaultdict(int)
    if teacher_limits_override is not None:
        teacher_limits = dict(teacher_limits_override)
    else:
        teacher_limits = {}
    for e in entries:
        tid = changed_teacher(e)
        teacher_daily[(tid, changed_day(e))] += 1
        if teacher_limits_override is None:
            limit = e.get('teacher_max_daily')
            if limit is not None:
                teacher_limits[tid] = limit

    for (tid, day), count in teacher_daily.items():
        limit = teacher_limits.get(tid)
        if limit is not None and count > limit and (tid, day) in affected_teacher_days:
            violations.append(('teacher_cap', tid, day, count, limit))

    # 同一门课在一个班每天节数上限
    cc_daily = defaultdict(int)
    course_limits = {}
    for e in entries:
        cc_daily[(e['class_id'], e['course'], changed_day(e))] += 1
        if e.get('course_max_daily') is not None:
            course_limits[e['course']] = e['course_max_daily']

    for (clid, cid, day), count in cc_daily.items():
        limit = course_limits.get(cid)
        if limit is not None and count > limit \
                and (clid, cid, day) in affected_class_course_days:
            violations.append(('course_cap', clid, cid, day, count, limit))

    # 基础时间冲突（同一教师/教室/班级在同一时段），只看受影响时段
    slot_teachers = defaultdict(int)
    slot_classrooms = defaultdict(int)
    slot_classes = defaultdict(int)
    for e in entries:
        key = (changed_day(e), changed_period(e))
        if key not in affected_slots:
            continue
        slot_teachers[(changed_teacher(e),) + key] += 1
        slot_classrooms[(e['classroom'],) + key] += 1
        slot_classes[(e['class_id'],) + key] += 1

    for (tid, day, period), count in slot_teachers.items():
        if count > 1:
            violations.append(('time_conflict', 'teacher', day, period, count))
    for (room, day, period), count in slot_classrooms.items():
        if count > 1:
            violations.append(('time_conflict', 'classroom', day, period, count))
    for (clid, day, period), count in slot_classes.items():
        if count > 1:
            violations.append(('time_conflict', 'class', day, period, count))

    return violations, changed_ids


def detect_cap_violations(entries: List[Dict]):
    """扫描一份课表中所有违反每日上限的条目（含锁定课、历史存量）。

    :return: 结构化违例列表，元组结构同 validate_cap_change
    """
    violations = []
    teacher_daily = defaultdict(int)
    cc_daily = defaultdict(int)
    teacher_limits = {}
    course_limits = {}

    for e in entries:
        teacher_daily[(e['teacher_id'], e['day_of_week'])] += 1
        cc_daily[(e['class_id'], e['course'], e['day_of_week'])] += 1
        if e.get('teacher_max_daily') is not None:
            teacher_limits[e['teacher_id']] = e['teacher_max_daily']
        if e.get('course_max_daily') is not None:
            course_limits[e['course']] = e['course_max_daily']

    for (tid, day), count in teacher_daily.items():
        limit = teacher_limits.get(tid)
        if limit is not None and count > limit:
            violations.append(('teacher_cap', tid, day, count, limit))
    for (clid, cid, day), count in cc_daily.items():
        limit = course_limits.get(cid)
        if limit is not None and count > limit:
            violations.append(('course_cap', clid, cid, day, count, limit))
    return violations


def format_cap_violations(violations, teacher_names=None, course_names=None,
                          class_names=None) -> List[str]:
    """把 validate_cap_change 的结构化违例转成中文提示。"""
    teacher_names = teacher_names or {}
    course_names = course_names or {}
    class_names = class_names or {}
    messages = []

    for v in violations:
        kind = v[0]
        if kind == 'teacher_cap':
            _, tid, day, count, limit = v
            tname = teacher_names.get(tid) or f'教师{tid}'
            messages.append(
                f"{tname}在{weekday_name(day)}已有 {count} 节课，"
                f"超过每天最多 {limit} 节的上限"
            )
        elif kind == 'course_cap':
            _, clid, cid, day, count, limit = v
            cname = course_names.get(cid) or f'课程{cid}'
            clname = class_names.get(clid) or f'班级{clid}'
            messages.append(
                f"{clname}的{cname}在{weekday_name(day)}已排 {count} 节，"
                f"超过该课程每班每天最多 {limit} 节的上限"
            )
        elif kind == 'time_conflict':
            _, who, day, period, count = v
            who_label = {
                'teacher': '教师', 'classroom': '教室', 'class': '班级'
            }.get(who, who)
            messages.append(
                f"{who_label}在{weekday_name(day)}第{period}节时间冲突"
                f"（同一时段有 {count} 门课）"
            )
    return messages


class ConflictDetector:
    def detect_conflicts(self, entries: List[Dict]) -> List[Dict]:
        conflicts = []
        by_slot = defaultdict(list)

        for entry in entries:
            key = (entry['day_of_week'], entry['period'])
            by_slot[key].append(entry)

        for (day, period), slot_entries in by_slot.items():
            teacher_map = defaultdict(list)
            classroom_map = defaultdict(list)
            class_map = defaultdict(list)

            for entry in slot_entries:
                teacher_map[entry['teacher_id']].append(entry)
                classroom_map[entry['classroom_id']].append(entry)
                class_map[entry['class_id']].append(entry)

            for tid, t_entries in teacher_map.items():
                if len(t_entries) > 1:
                    conflicts.append({
                        'conflict_type': 'teacher',
                        'day_of_week': day,
                        'period': period,
                        'involved_entries': [e.get('id') for e in t_entries if e.get('id')],
                        'message': f"教师 {tid} 同一时间有 {len(t_entries)} 门课"
                    })

            for cid, c_entries in classroom_map.items():
                if len(c_entries) > 1:
                    conflicts.append({
                        'conflict_type': 'classroom',
                        'day_of_week': day,
                        'period': period,
                        'involved_entries': [e.get('id') for e in c_entries if e.get('id')],
                        'message': f"教室 {cid} 同一时间有 {len(c_entries)} 门课"
                    })

            for clid, cl_entries in class_map.items():
                if len(cl_entries) > 1:
                    conflicts.append({
                        'conflict_type': 'class',
                        'day_of_week': day,
                        'period': period,
                        'involved_entries': [e.get('id') for e in cl_entries if e.get('id')],
                        'message': f"班级 {clid} 同一时间有 {len(cl_entries)} 门课"
                    })

        return conflicts
