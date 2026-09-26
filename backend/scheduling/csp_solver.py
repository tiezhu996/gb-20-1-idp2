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
    max_daily_per_class: Optional[int] = None
    course_name: str = ''
    class_name: str = ''
    locked_hours: int = 0
    total_weekly_hours: int = 0


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
        # 教师每天已排节数: (teacher_id, day) -> count
        self.teacher_daily_count = defaultdict(int)
        # 同一门课在某个班每天已排节数: (class_id, course_id, day) -> count
        self.course_daily_count = defaultdict(int)
        # 教师每天最多节数: teacher_id -> 上限（None 表示不限制）
        self.teacher_daily_limits: Dict[int, Optional[int]] = {}
        self.assignments = []
        self.conflicts = []

    def generate_time_slots_for_priority(self, priority: str) -> List[TimeSlot]:
        morning_periods = min(4, self.daily_periods)
        morning_slots = [
            s for s in self.all_slots
            if s.period <= morning_periods
        ]
        afternoon_slots = [
            s for s in self.all_slots
            if s.period > morning_periods
        ]

        if priority == 'high':
            return morning_slots + afternoon_slots
        elif priority == 'medium':
            return random.sample(self.all_slots, len(self.all_slots))
        else:
            return afternoon_slots + morning_slots

    def is_available(
        self,
        time_slot: TimeSlot,
        teacher_id: int,
        class_id: int,
        classroom_id: int,
        teacher_available_slots: Set[TimeSlot],
        course_id: Optional[int] = None,
        max_daily_per_class: Optional[int] = None
    ) -> bool:
        if teacher_available_slots and time_slot not in teacher_available_slots:
            return False
        if time_slot in self.teacher_usage[teacher_id]:
            return False
        if time_slot in self.class_usage[class_id]:
            return False
        if time_slot in self.classroom_usage[classroom_id]:
            return False
        # 教师每天最多上几节课
        teacher_limit = self.teacher_daily_limits.get(teacher_id)
        if teacher_limit is not None:
            if self.teacher_daily_count[(teacher_id, time_slot.day)] >= teacher_limit:
                return False
        # 同一门课在一个班每天最多排几节
        if course_id is not None and max_daily_per_class is not None:
            if self.course_daily_count[(class_id, course_id, time_slot.day)] >= max_daily_per_class:
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
        self.teacher_daily_count.clear()
        self.course_daily_count.clear()
        self.teacher_daily_limits = {
            tid: tdata.get('max_daily_lessons')
            for tid, tdata in teachers_data.items()
        }

        if locked_entries:
            for entry in locked_entries:
                slot = TimeSlot(day=entry['day_of_week'], period=entry['period'])
                self.classroom_usage[entry['classroom_id']].add(slot)
                self.teacher_usage[entry['teacher_id']].add(slot)
                self.class_usage[entry['class_id']].add(slot)
                self.teacher_daily_count[(entry['teacher_id'], slot.day)] += 1
                self.course_daily_count[
                    (entry['class_id'], entry['course_id'], slot.day)
                ] += 1
                self.assignments.append(entry)

        priority_order = {'high': 0, 'medium': 1, 'low': 2}
        sorted_tasks = sorted(
            tasks,
            key=lambda t: (priority_order[t.priority], -t.weekly_hours)
        )

        for task in sorted_tasks:
            hours_assigned = 0
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

            task_label = f"班级{task.class_name or task.class_id}的{task.course_name or task.course_id}课"

            if not compatible_rooms:
                self.conflicts.append({
                    'type': 'classroom',
                    'task': task_label,
                    'message': f"{task_label}：没有找到适合 {task.preferred_room_type} 类型的教室"
                })
                continue

            candidate_slots = self.generate_time_slots_for_priority(task.priority)

            while hours_assigned < task.weekly_hours:
                # 找出所有仍然可行的时间段+教室，不再随机硬试
                feasible = []
                for slot in candidate_slots:
                    for room in compatible_rooms:
                        if self.is_available(
                            slot,
                            task.teacher_id,
                            task.class_id,
                            room,
                            teacher_available,
                            course_id=task.course_id,
                            max_daily_per_class=task.max_daily_per_class
                        ):
                            feasible.append((slot, room))

                if not feasible:
                    break

                slot, available_room = random.choice(feasible)
                self.classroom_usage[available_room].add(slot)
                self.teacher_usage[task.teacher_id].add(slot)
                self.class_usage[task.class_id].add(slot)
                self.teacher_daily_count[(task.teacher_id, slot.day)] += 1
                self.course_daily_count[(task.class_id, task.course_id, slot.day)] += 1

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
                total = task.total_weekly_hours or (task.locked_hours + task.weekly_hours)
                scheduled_total = task.locked_hours + hours_assigned
                reasons = []
                teacher_limit = self.teacher_daily_limits.get(task.teacher_id)
                if teacher_limit is not None:
                    reasons.append(f"教师每天最多 {teacher_limit} 节")
                if task.max_daily_per_class is not None:
                    reasons.append(f"该课每班每天最多 {task.max_daily_per_class} 节")
                reason_text = '；'.join(reasons) if reasons else '无可用时段'
                self.conflicts.append({
                    'type': 'insufficient_slots',
                    'task': task_label,
                    'message': (
                        f"{task_label}：仅安排了 {scheduled_total}/{total} 课时"
                        f"（含已锁定 {task.locked_hours} 节），"
                        f"剩余课时受上限约束（{reason_text}），未强行排入"
                    )
                })

        return self.assignments, self.conflicts


class ConflictDetector:
    def detect_conflicts(self, entries: List[Dict]) -> List[Dict]:
        conflicts = []
        conflicts.extend(self._detect_slot_conflicts(entries))
        conflicts.extend(self._detect_daily_limit_violations(entries))
        return conflicts

    def _detect_slot_conflicts(self, entries: List[Dict]) -> List[Dict]:
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

    def _detect_daily_limit_violations(self, entries: List[Dict]) -> List[Dict]:
        """检测突破每日上限的排课：教师每天最多节数、同一门课每班每天最多节数。"""
        from core.models import Course, Teacher

        conflicts = []
        teacher_limits = dict(Teacher.objects.values_list('id', 'max_daily_lessons'))
        course_limits = dict(Course.objects.values_list('id', 'max_daily_per_class'))
        teacher_names = dict(Teacher.objects.values_list('id', 'name'))
        course_names = dict(Course.objects.values_list('id', 'name'))
        weekday_cn = {1: '一', 2: '二', 3: '三', 4: '四', 5: '五',
                      6: '六', 7: '日'}

        teacher_day = defaultdict(list)
        course_class_day = defaultdict(list)
        for entry in entries:
            teacher_day[(entry['teacher_id'], entry['day_of_week'])].append(entry)
            course_class_day[
                (entry['class_id'], entry['course_id'], entry['day_of_week'])
            ].append(entry)

        for (tid, day), day_entries in teacher_day.items():
            limit = teacher_limits.get(tid)
            if limit is not None and len(day_entries) > limit:
                tname = teacher_names.get(tid, tid)
                periods = sorted(e['period'] for e in day_entries)
                conflicts.append({
                    'conflict_type': 'teacher_daily_limit',
                    'day_of_week': day,
                    'period': periods[-1],
                    'involved_entries': [e.get('id') for e in day_entries if e.get('id')],
                    'message': (
                        f"教师 {tname} 周{weekday_cn.get(day, day)}当天有 "
                        f"{len(day_entries)} 节课（第{','.join(map(str, periods))}节），"
                        f"超过每天最多 {limit} 节的上限"
                    )
                })

        for (clid, coid, day), day_entries in course_class_day.items():
            limit = course_limits.get(coid)
            if limit is not None and len(day_entries) > limit:
                cname = course_names.get(coid, coid)
                periods = sorted(e['period'] for e in day_entries)
                conflicts.append({
                    'conflict_type': 'course_daily_limit',
                    'day_of_week': day,
                    'period': periods[-1],
                    'involved_entries': [e.get('id') for e in day_entries if e.get('id')],
                    'message': (
                        f"{cname} 在班级 {clid} 周{weekday_cn.get(day, day)}当天排了 "
                        f"{len(day_entries)} 节（第{','.join(map(str, periods))}节），"
                        f"超过同一门课每班每天最多 {limit} 节的上限"
                    )
                })

        return conflicts
