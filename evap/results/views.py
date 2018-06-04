from collections import OrderedDict, namedtuple

from evap.evaluation.auth import staff_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render
from django.contrib.auth.decorators import login_required
from django.forms import formset_factory
from django.http import HttpResponse
from django.utils.translation import ugettext as _

from evap.evaluation.models import Semester, Degree, Contribution
from evap.evaluation.auth import internal_required
from evap.results.tools import calculate_results, calculate_average_distribution, distribution_to_grade, \
    TextResult, RatingResult, HeadingResult, COMMENT_STATES_REQUIRED_FOR_VISIBILITY, YesNoResult


@internal_required
def index(request):
    semesters = Semester.get_all_with_published_courses()

    return render(request, "results_index.html", dict(semesters=semesters))


@internal_required
def semester_detail(request, semester_id):
    semester = get_object_or_404(Semester, id=semester_id)

    visible_states = ['published']
    if request.user.is_reviewer:
        visible_states += ['in_evaluation', 'evaluated', 'reviewed']

    courses = semester.course_set.filter(state__in=visible_states).prefetch_related("degrees")

    courses = [course for course in courses if course.can_user_see_course(request.user)]

    for course in courses:
        course.distribution = calculate_average_distribution(course) if course.can_user_see_grades(request.user) else None
        course.avg_grade = distribution_to_grade(course.distribution)

    CourseTuple = namedtuple('CourseTuple', ('courses', 'single_results'))

    courses_by_degree = OrderedDict()
    for degree in Degree.objects.all():
        courses_by_degree[degree] = CourseTuple([], [])
    for course in courses:
        if course.is_single_result:
            for degree in course.degrees.all():
                section = calculate_results(course)[0]
                result = section.results[0]
                courses_by_degree[degree].single_results.append((course, result))
        else:
            for degree in course.degrees.all():
                courses_by_degree[degree].courses.append(course)

    template_data = dict(semester=semester, courses_by_degree=courses_by_degree)
    return render(request, "results_semester_detail.html", template_data)


@login_required
def course_detail(request, semester_id, course_id):
    semester = get_object_or_404(Semester, id=semester_id)
    course = get_object_or_404(semester.course_set, id=course_id, semester=semester)

    if not course.can_user_see_results_page(request.user):
        raise PermissionDenied

    sections = calculate_results(course)

    if request.user.is_reviewer:
        public_view = request.GET.get('public_view') != 'false'  # if parameter is not given, show public view.
    else:
        public_view = request.GET.get('public_view') == 'true'  # if parameter is not given, show own view.

    # If grades are not published, there is no public view
    if not course.has_enough_voters_to_publish_grades:
        public_view = False

    represented_users = list(request.user.represented_users.all())
    represented_users.append(request.user)

    show_grades = course.can_user_see_grades(request.user)

    # remove text answers and grades if the user may not see them
    for section in sections:
        results = []
        for result in section.results:
            if isinstance(result, TextResult):
                answers = [answer for answer in result.answers if user_can_see_text_answer(request.user, represented_users, answer, public_view)]
                if answers:
                    results.append(TextResult(question=result.question, answers=answers))
            elif isinstance(result, RatingResult) and not show_grades:
                results.append(RatingResult(question=result.question, total_count=result.total_count, average=None, counts=None, warning=result.warning))
            elif isinstance(result, YesNoResult) and not show_grades:
                results.append(YesNoResult(question=result.question, total_count=result.total_count, average=None, counts=None, warning=result.warning, approval_count=None))
            else:
                results.append(result)

        section.results[:] = results

    # filter empty headings
    for section in sections:
        filtered_results = []
        for index in range(len(section.results)):
            result = section.results[index]
            # filter out if there are no more questions or the next question is also a heading question
            if isinstance(result, HeadingResult):
                if index == len(section.results) - 1 or isinstance(section.results[index + 1], HeadingResult):
                    continue
            filtered_results.append(result)
        section.results[:] = filtered_results

    # remove empty sections
    sections = [section for section in sections if section.results]

    # group by contributor
    course_sections_top = []
    course_sections_bottom = []
    contributor_sections = OrderedDict()
    for section in sections:
        if section.contributor is None:
            if section.questionnaire.is_below_contributors:
                course_sections_bottom.append(section)
            else:
                course_sections_top.append(section)
        else:
            contributor_sections.setdefault(section.contributor,
                                            {'total_votes': 0, 'sections': []})['sections'].append(section)

            for result in section.results:
                if isinstance(result, TextResult):
                    contributor_sections[section.contributor]['total_votes'] += 1
                elif isinstance(result, RatingResult) or isinstance(result, YesNoResult):
                    # Only count rating results if we show the grades.
                    if show_grades:
                        contributor_sections[section.contributor]['total_votes'] += result.total_count

    course.distribution = calculate_average_distribution(course) if show_grades else None
    course.avg_grade = distribution_to_grade(course.distribution)

    template_data = dict(
            course=course,
            course_sections_top=course_sections_top,
            course_sections_bottom=course_sections_bottom,
            contributor_sections=contributor_sections,
            reviewer=request.user.is_reviewer,
            contributor=course.is_user_contributor_or_delegate(request.user),
            can_download_grades=request.user.can_download_grades,
            public_view=public_view)
    return render(request, "results_course_detail.html", template_data)


@staff_required
def semester_raw_export(request, semester_id):
    semester = get_object_or_404(Semester, id=semester_id)

    filename = "Evaluation-{}-{}_raw.csv".format(semester.name, get_language())
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = "attachment; filename=\"{}\"".format(filename)

    writer = csv.writer(response, delimiter=";")
    writer.writerow([_('Name'), _('Degrees'), _('Type'), _('Single result'), _('State'), _('#Voters'),
        _('#Participants'), _('#Comments'), _('Average grade')])
    for course in semester.course_set.all():
        degrees = ", ".join([degree.name for degree in course.degrees.all()])
        course.avg_grade, course.avg_deviation = calculate_average_grades_and_deviation(course)
        if course.state in ['evaluated', 'reviewed', 'published'] and course.avg_grade is not None:
            avg_grade = "{:.1f}".format(course.avg_grade)
        else:
            avg_grade = ""
        writer.writerow([course.name, degrees, course.type.name, course.is_single_result, course.state,
            course.num_voters, course.num_participants, course.textanswer_set.count(), avg_grade])

    return response


@staff_required
def semester_export(request, semester_id):
    semester = get_object_or_404(Semester, id=semester_id)

    ExportSheetFormset = formset_factory(form=ExportSheetForm, can_delete=True, extra=0, min_num=1, validate_min=True)
    formset = ExportSheetFormset(request.POST or None, form_kwargs={'semester': semester})

    if formset.is_valid():
        include_not_enough_answers = request.POST.get('include_not_enough_answers') == 'on'
        include_unpublished = request.POST.get('include_unpublished') == 'on'
        course_types_list = []
        for form in formset:
            if 'selected_course_types' in form.cleaned_data:
                course_types_list.append(form.cleaned_data['selected_course_types'])

        filename = "Evaluation-{}-{}.xls".format(semester.name, get_language())
        response = HttpResponse(content_type="application/vnd.ms-excel")
        response["Content-Disposition"] = "attachment; filename=\"{}\"".format(filename)
        ExcelExporter(semester).export(response, course_types_list, include_not_enough_answers, include_unpublished)
        return response
    else:
        return render(request, "staff_semester_export.html", dict(semester=semester, formset=formset))

def user_can_see_text_answer(user, represented_users, text_answer, public_view=False):
    if public_view:
        return False
    if text_answer.state not in COMMENT_STATES_REQUIRED_FOR_VISIBILITY:
        return False
    if user.is_reviewer:
        return True

    contributor = text_answer.contribution.contributor

    if text_answer.is_private:
        return contributor == user

    if text_answer.is_published:
        if text_answer.contribution.responsible:
            return contributor == user or user in contributor.delegates.all()

        if contributor in represented_users:
            return True
        if text_answer.contribution.course.contributions.filter(
                contributor__in=represented_users, comment_visibility=Contribution.ALL_COMMENTS).exists():
            return True
        if text_answer.contribution.is_general and text_answer.contribution.course.contributions.filter(
                contributor__in=represented_users, comment_visibility=Contribution.COURSE_COMMENTS).exists():
            return True

    return False
