from django.http import HttpResponse
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from django.db import transaction
from core.models import Semester, Classroom, Teacher, Class, Course
from .models import (
    ClassCourse, ScheduleEntry, Conflict, SwapRequest, Substitute
)
from .serializers import (
    ClassCourseSerializer, ScheduleEntrySerializer,
    ScheduleEntryDetailSerializer, ConflictSerializer,
    SwapRequestSerializer, SubstituteSerializer,
    AutoScheduleRequestSerializer, ConflictCheckSerializer,
    SwapScheduleRequestSerializer, SubstituteRequestSerializer
)
from .csp_solver import (
    CSPScheduler, ConflictDetector, SchedulingTask,
    validate_cap_change, detect_cap_violations, format_cap_violations, weekday_name
)
from .pdf_export import (
    generate_class_timetable_pdf,
    generate_teacher_timetable_pdf,
    generate_classroom_timetable_pdf
)


class ClassCourseViewSet(viewsets.ModelViewSet):
    queryset = ClassCourse.objects.all()
    serializer_class = ClassCourseSerializer
    permission_classes = [AllowAny]


class ScheduleEntryViewSet(viewsets.ModelViewSet):
    queryset = ScheduleEntry.objects.all().select_related(
        'course', 'teacher', 'classroom', 'class_id', 'semester'
    )
    serializer_class = ScheduleEntryDetailSerializer
    permission_classes = [AllowAny]

    def get_serializer_class(self):
        if self.action in ['list', 'retrieve']:
            return ScheduleEntryDetailSerializer
        return ScheduleEntrySerializer

    def update(self, request, *args, **kwargs):
        """单条更新若涉及时间/教师变动，同样按每日上限复核，越界整笔不动。"""
        partial = kwargs.pop('partial', False)
        instance = self.get_object()

        day = request.data.get('day_of_week', instance.day_of_week)
        period = request.data.get('period', instance.period)
        new_teacher_id = request.data.get('teacher', instance.teacher_id)

        moves_made = (
            int(day) != instance.day_of_week
            or int(period) != instance.period
            or int(new_teacher_id) != instance.teacher_id
        )
        if moves_made:
            error = self._validate_entry_changes(
                [(instance, int(day), int(period),
                  int(new_teacher_id) if int(new_teacher_id) != instance.teacher_id else None)]
            )
            if error:
                return Response(error, status=status.HTTP_400_BAD_REQUEST)

        return super().update(request, *args, partial=partial, **kwargs)

    @action(detail=False, methods=['get'])
    def by_semester(self, request):
        semester_id = request.query_params.get('semester_id')
        if not semester_id:
            return Response(
                {'error': 'semester_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        entries = self.queryset.filter(semester_id=semester_id)
        serializer = ScheduleEntryDetailSerializer(entries, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def by_class(self, request):
        semester_id = request.query_params.get('semester_id')
        class_id = request.query_params.get('class_id')
        entries = self.queryset.filter(semester_id=semester_id, class_id=class_id)
        serializer = ScheduleEntryDetailSerializer(entries, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def by_teacher(self, request):
        semester_id = request.query_params.get('semester_id')
        teacher_id = request.query_params.get('teacher_id')
        entries = self.queryset.filter(semester_id=semester_id, teacher_id=teacher_id)
        serializer = ScheduleEntryDetailSerializer(entries, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'])
    def by_classroom(self, request):
        semester_id = request.query_params.get('semester_id')
        classroom_id = request.query_params.get('classroom_id')
        entries = self.queryset.filter(semester_id=semester_id, classroom_id=classroom_id)
        serializer = ScheduleEntryDetailSerializer(entries, many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['post'])
    def auto_schedule(self, request):
        req_serializer = AutoScheduleRequestSerializer(data=request.data)
        if not req_serializer.is_valid():
            return Response(req_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        semester_id = req_serializer.validated_data['semester_id']
        respect_locked = req_serializer.validated_data['respect_locked']

        try:
            semester = Semester.objects.get(id=semester_id)
        except Semester.DoesNotExist:
            return Response(
                {'error': 'Semester not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        class_courses = ClassCourse.objects.filter(
            semester=semester
        ).select_related('class_id', 'course', 'teacher')

        if not class_courses.exists():
            return Response(
                {'error': 'No class courses configured for this semester'},
                status=status.HTTP_400_BAD_REQUEST
            )

        tasks = []
        for cc in class_courses:
            tasks.append(SchedulingTask(
                class_id=cc.class_id.id,
                course_id=cc.course.id,
                teacher_id=cc.teacher.id,
                weekly_hours=cc.course.weekly_hours,
                preferred_room_type=cc.course.preferred_room_type,
                priority=cc.course.priority,
                available_time_slots=[],
                classroom_capacity=cc.class_id.student_count or 40,
                teacher_max_daily=cc.teacher.max_daily_lessons,
                course_max_daily=cc.course.max_daily_per_class,
                class_name=str(cc.class_id),
                course_name=cc.course.name
            ))

        classrooms_data = {
            c.id: {
                'room_type': c.room_type,
                'capacity': c.capacity,
                'name': c.name
            } for c in Classroom.objects.filter(is_active=True)
        }

        teachers_data = {
            t.id: {
                'name': t.name,
                'available_time_slots': t.available_time_slots if t.available_time_slots else []
            } for t in Teacher.objects.filter(is_active=True)
        }

        locked_entries = []
        if respect_locked:
            locked = ScheduleEntry.objects.filter(
                semester=semester, is_locked=True
            ).values(
                'id', 'class_id', 'course_id', 'teacher_id', 'classroom_id',
                'day_of_week', 'period', 'is_locked'
            )
            locked_entries = list(locked)

        scheduler = CSPScheduler(semester)
        assignments, scheduling_conflicts = scheduler.schedule(
            tasks, classrooms_data, teachers_data, locked_entries
        )

        with transaction.atomic():
            if respect_locked:
                ScheduleEntry.objects.filter(
                    semester=semester, is_locked=False
                ).delete()
            else:
                ScheduleEntry.objects.filter(semester=semester).delete()

            bulk_entries = []
            for a in assignments:
                if a.get('is_locked'):
                    continue
                bulk_entries.append(ScheduleEntry(
                    semester_id=a['semester_id'],
                    class_id_id=a['class_id'],
                    course_id=a['course_id'],
                    teacher_id=a['teacher_id'],
                    classroom_id=a['classroom_id'],
                    day_of_week=a['day_of_week'],
                    period=a['period'],
                    is_locked=False
                ))
            ScheduleEntry.objects.bulk_create(bulk_entries)

            all_entries = ScheduleEntry.objects.filter(
                semester=semester
            ).values('id', 'teacher_id', 'classroom_id', 'class_id', 'day_of_week', 'period')

            detector = ConflictDetector()
            conflicts = detector.detect_conflicts(list(all_entries))

            Conflict.objects.filter(semester=semester).delete()
            bulk_conflicts = []
            for c in conflicts:
                bulk_conflicts.append(Conflict(
                    semester=semester,
                    conflict_type=c['conflict_type'],
                    day_of_week=c['day_of_week'],
                    period=c['period'],
                    involved_entries=c['involved_entries'],
                    message=c['message']
                ))
            Conflict.objects.bulk_create(bulk_conflicts)

            for c in conflicts:
                for eid in c['involved_entries']:
                    try:
                        entry = ScheduleEntry.objects.get(id=eid)
                        entry.is_conflict = True
                        entry.conflict_type = c['conflict_type']
                        entry.save()
                    except ScheduleEntry.DoesNotExist:
                        pass

        # 扫描每日上限违例（新排的课不会越界，这里主要暴露锁定课/存量问题）
        final_qs = ScheduleEntry.objects.filter(
            semester=semester
        ).select_related('teacher', 'course', 'class_id')
        cap_entry_dicts = [{
            'teacher_id': e.teacher_id,
            'class_id': e.class_id_id,
            'course': e.course_id,
            'day_of_week': e.day_of_week,
            'teacher_name': e.teacher.name,
            'course_name': e.course.name,
            'class_name': str(e.class_id),
            'teacher_max_daily': e.teacher.max_daily_lessons,
            'course_max_daily': e.course.max_daily_per_class,
        } for e in final_qs]
        cap_violations = detect_cap_violations(cap_entry_dicts)
        cap_messages = format_cap_violations(
            cap_violations,
            teacher_names={t.id: t.name for t in Teacher.objects.all()},
            course_names={c.id: c.name for c in Course.objects.all()},
            class_names={c.id: str(c) for c in Class.objects.all()}
        )

        final_entries = ScheduleEntry.objects.filter(semester=semester)
        serializer = ScheduleEntryDetailSerializer(final_entries, many=True)

        return Response({
            'schedule': serializer.data,
            'conflicts': conflicts,
            'scheduling_messages': scheduling_conflicts,
            'limit_violations': cap_messages,
            'total_entries': len(serializer.data)
        })

    @action(detail=False, methods=['post'])
    def check_conflicts(self, request):
        req_serializer = ConflictCheckSerializer(data=request.data)
        if not req_serializer.is_valid():
            return Response(req_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        semester_id = req_serializer.validated_data['semester_id']
        entries = ScheduleEntry.objects.filter(
            semester_id=semester_id
        ).values('id', 'teacher_id', 'classroom_id', 'class_id', 'day_of_week', 'period')

        detector = ConflictDetector()
        conflicts = detector.detect_conflicts(list(entries))

        return Response({'conflicts': conflicts})

    @action(detail=False, methods=['post'])
    def swap(self, request):
        req_serializer = SwapScheduleRequestSerializer(data=request.data)
        if not req_serializer.is_valid():
            return Response(req_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        entry1_id = req_serializer.validated_data['entry1_id']
        entry2_id = req_serializer.validated_data['entry2_id']
        reason = req_serializer.validated_data.get('reason', '')

        try:
            entry1 = ScheduleEntry.objects.get(id=entry1_id)
            entry2 = ScheduleEntry.objects.get(id=entry2_id)
        except ScheduleEntry.DoesNotExist:
            return Response(
                {'error': 'One or both entries not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        if entry1.semester_id != entry2.semester_id:
            return Response(
                {'error': '两门课不属于同一学期，不能调课',
                 'detail': f"{entry1.course.name} 与 {entry2.course.name}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        error = self._validate_entry_changes([
            (entry1, entry2.day_of_week, entry2.period, None),
            (entry2, entry1.day_of_week, entry1.period, None),
        ])
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            day1, period1 = entry1.day_of_week, entry1.period
            day2, period2 = entry2.day_of_week, entry2.period

            entry1.day_of_week, entry1.period = day2, period2
            entry2.day_of_week, entry2.period = day1, period1

            entry1.save()
            entry2.save()

            if reason:
                SwapRequest.objects.create(
                    semester=entry1.semester,
                    requesting_teacher=entry1.teacher,
                    target_teacher=entry2.teacher,
                    entry1=entry1,
                    entry2=entry2,
                    reason=reason,
                    status='approved'
                )

        return Response({'status': 'success', 'message': 'Swap completed'})

    @action(detail=False, methods=['post'])
    def substitute(self, request):
        req_serializer = SubstituteRequestSerializer(data=request.data)
        if not req_serializer.is_valid():
            return Response(req_serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        entry_id = req_serializer.validated_data['entry_id']
        substitute_teacher_id = req_serializer.validated_data['substitute_teacher_id']
        start_date = req_serializer.validated_data['start_date']
        end_date = req_serializer.validated_data['end_date']
        reason = req_serializer.validated_data['reason']

        try:
            entry = ScheduleEntry.objects.get(id=entry_id)
            substitute_teacher = Teacher.objects.get(id=substitute_teacher_id)
        except (ScheduleEntry.DoesNotExist, Teacher.DoesNotExist):
            return Response(
                {'error': 'Entry or teacher not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        if substitute_teacher_id == entry.teacher_id:
            return Response(
                {'error': '代课教师不能与原教师相同',
                 'detail': f"{entry.course.name} 周{entry.day_of_week}第{entry.period}节"},
                status=status.HTTP_400_BAD_REQUEST
            )

        if entry.original_teacher_id is not None:
            return Response(
                {'error': '该课程已安排代课，不能重复代课；如需更换请先取消原代课',
                 'detail': f"{entry.course.name} 周{entry.day_of_week}第{entry.period}节，"
                           f"当前代课教师：{entry.teacher.name}"},
                status=status.HTTP_400_BAD_REQUEST
            )

        error = self._validate_entry_changes(
            [(entry, entry.day_of_week, entry.period, substitute_teacher_id)]
        )
        if error:
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

        original_teacher = entry.teacher

        with transaction.atomic():
            Substitute.objects.create(
                semester=entry.semester,
                original_teacher=original_teacher,
                substitute_teacher=substitute_teacher,
                affected_entry=entry,
                start_date=start_date,
                end_date=end_date,
                reason=reason
            )

            entry.original_teacher = original_teacher
            entry.teacher = substitute_teacher
            entry.save()

        serializer = ScheduleEntryDetailSerializer(entry)
        return Response({'status': 'success', 'entry': serializer.data})

    @staticmethod
    def _load_semester_entry_dicts(semester):
        """取学期内全部条目（含每日上限与名称），供上限校验使用。"""
        qs = ScheduleEntry.objects.filter(
            semester=semester
        ).select_related('course', 'teacher', 'class_id', 'classroom')
        entries = []
        for e in qs:
            entries.append({
                'id': e.id,
                'class_id': e.class_id_id,
                'course': e.course_id,
                'teacher_id': e.teacher_id,
                'classroom': e.classroom_id,
                'day_of_week': e.day_of_week,
                'period': e.period,
                'teacher_name': e.teacher.name,
                'course_name': e.course.name,
                'class_name': str(e.class_id),
                'classroom_name': e.classroom.name,
                'teacher_max_daily': e.teacher.max_daily_lessons,
                'course_max_daily': e.course.max_daily_per_class,
            })
        return entries

    def _validate_entry_changes(self, moves):
        """校验调课/代课移动是否越界。

        :param moves: [(ScheduleEntry, new_day, new_period, new_teacher_id_or_None)]
        :return: 校验通过返回 None；否则返回可直接响应的错误 dict（整笔不动）
        """
        semester = moves[0][0].semester
        entries = self._load_semester_entry_dicts(semester)

        changes = {}
        for entry_obj, new_day, new_period, new_teacher_id in moves:
            changes[entry_obj.id] = (new_day, new_period, new_teacher_id)

        # 教师上限一律以教师表当前配置为准（含代课教师、原任课教师）
        involved_teacher_ids = {e['teacher_id'] for e in entries}
        for _, _, _, new_teacher_id in moves:
            if new_teacher_id is not None:
                involved_teacher_ids.add(new_teacher_id)
        teacher_limits_map = dict(
            Teacher.objects.filter(id__in=involved_teacher_ids)
            .values_list('id', 'max_daily_lessons')
        )

        violations, _ = validate_cap_change(
            entries, changes, teacher_limits_override=teacher_limits_map
        )
        if not violations:
            return None

        teacher_names = dict(
            Teacher.objects.filter(id__in=involved_teacher_ids).values_list('id', 'name')
        )
        course_names = {e['course']: e['course_name'] for e in entries}
        class_names = {e['class_id']: e['class_name'] for e in entries}

        messages = format_cap_violations(
            violations, teacher_names, course_names, class_names
        )

        # 附上涉及的课程、星期几、第几节，方便教务定位
        details = [
            f"{entry_obj.course.name}（{weekday_name(new_day)}第{new_period}节）"
            for entry_obj, new_day, new_period, _ in moves
        ]

        return {
            'error': '调课/代课校验未通过，整笔操作未执行',
            'violations': messages,
            'detail': '涉及课程：' + '、'.join(details),
            'message': '；'.join(messages)
        }

    @action(detail=False, methods=['get'])
    def export_pdf(self, request):
        semester_id = request.query_params.get('semester_id')
        entity_type = request.query_params.get('type')
        entity_id = request.query_params.get('id')

        try:
            semester = Semester.objects.get(id=semester_id)
        except Semester.DoesNotExist:
            return Response(
                {'error': 'Semester not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        pdf_buffer = None
        filename = 'timetable.pdf'

        try:
            if entity_type == 'class':
                class_obj = Class.objects.get(id=entity_id)
                pdf_buffer = generate_class_timetable_pdf(class_obj, semester)
                filename = f'{class_obj.name}_课表.pdf'
            elif entity_type == 'teacher':
                teacher = Teacher.objects.get(id=entity_id)
                pdf_buffer = generate_teacher_timetable_pdf(teacher, semester)
                filename = f'{teacher.name}_课表.pdf'
            elif entity_type == 'classroom':
                classroom = Classroom.objects.get(id=entity_id)
                pdf_buffer = generate_classroom_timetable_pdf(classroom, semester)
                filename = f'{classroom.name}_课表.pdf'
            else:
                return Response(
                    {'error': 'Invalid type. Must be class, teacher, or classroom'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        except (Class.DoesNotExist, Teacher.DoesNotExist, Classroom.DoesNotExist):
            return Response(
                {'error': 'Entity not found'},
                status=status.HTTP_404_NOT_FOUND
            )

        response = HttpResponse(pdf_buffer, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response


class ConflictViewSet(viewsets.ModelViewSet):
    queryset = Conflict.objects.all().select_related('semester')
    serializer_class = ConflictSerializer
    permission_classes = [AllowAny]


class SwapRequestViewSet(viewsets.ModelViewSet):
    queryset = SwapRequest.objects.all().select_related(
        'semester', 'requesting_teacher', 'target_teacher'
    )
    serializer_class = SwapRequestSerializer
    permission_classes = [AllowAny]


class SubstituteViewSet(viewsets.ModelViewSet):
    queryset = Substitute.objects.all().select_related(
        'semester', 'original_teacher', 'substitute_teacher'
    )
    serializer_class = SubstituteSerializer
    permission_classes = [AllowAny]
