# -*- coding: utf-8 -*-
##
## This file is part of Invenio.
## Copyright (C) 2013, 2014 CERN.
##
## Invenio is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## Invenio is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Invenio; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.
"""
Holding Pen

Holding Pen is an overlay over all objects (BibWorkflowObject) that
have run through a workflow (BibWorkflowEngine). This area is targeted
to catalogers and super users for inspecting ingestion workflows and
submissions/depositions.

Note: Currently work-in-progress.
"""

import re
import collections

from six import iteritems, text_type

from flask import (render_template, Blueprint, request, jsonify,
                   url_for, flash, session)
from flask.ext.login import login_required
from flask.ext.breadcrumbs import default_breadcrumb_root, register_breadcrumb
from flask.ext.menu import register_menu

from invenio.base.decorators import templated, wash_arguments
from invenio.base.i18n import _
from invenio.utils.date import pretty_date

from ..models import BibWorkflowObject, Workflow, ObjectVersion
from ..registry import actions, workflows
from ..utils import get_workflow_definition, sort_bwolist, WorkflowBase
from ..api import continue_oid_delayed, start_delayed

blueprint = Blueprint('holdingpen', __name__, url_prefix="/admin/holdingpen",
                      template_folder='../templates',
                      static_folder='../static')

default_breadcrumb_root(blueprint, '.holdingpen')

REG_TD = re.compile("<td title=\"(.+?)\">(.+?)</td>", re.DOTALL)


@blueprint.route('/', methods=['GET', 'POST'])
@blueprint.route('/index', methods=['GET', 'POST'])
@login_required
@register_menu(blueprint, 'personalize.holdingpen', _('Your Pending Actions'))
@register_breadcrumb(blueprint, '.', _('Holdingpen'))
@templated('workflows/hp_index.html')
def index():
    """
    Display main interface of Holdingpen.

    Acts as a hub for catalogers (may be removed)
    """
    # FIXME: Add user filtering
    bwolist = get_holdingpen_objects(version_showing=[ObjectVersion.HALTED])
    action_list = get_action_list(bwolist)

    return dict(tasks=action_list)


@blueprint.route('/maintable', methods=['GET', 'POST'])
@register_breadcrumb(blueprint, '.records', _('Records'))
@login_required
@templated('workflows/hp_maintable.html')
def maintable():
    """Display main table interface of Holdingpen."""
    bwolist = get_holdingpen_objects()
    action_list = get_action_list(bwolist)
    my_tags = []

    if 'holdingpen_tags' in session:
        my_tags = session["holdingpen_tags"]

    if 'version' in request.args:
        if ObjectVersion.MAPPING[int(request.args.get('version'))] not in my_tags:
            my_tags += ObjectVersion.MAPPING[[int(request.args.get('version'))]]
    tags_to_print = ""
    for tag in my_tags:
        if tag:
            tags_to_print += tag + ','
    return dict(bwolist=bwolist,
                action_list=action_list,
                tags=tags_to_print)


@blueprint.route('/batch_action', methods=['GET', 'POST'])
@login_required
@wash_arguments({'bwolist': (text_type, "")})
def batch_action(bwolist):
    """Render action accepting single or multiple records."""
    from ..utils import parse_bwids

    bwolist = parse_bwids(bwolist)

    try:
        bwolist = map(int, bwolist)
    except ValueError:
        # Bad ID, we just pass for now
        pass

    objlist = []
    workflow_func_list = []
    w_metadata_list = []
    info_list = []
    actionlist = []
    bwo_parent_list = []
    logtext_list = []

    objlist = [BibWorkflowObject.query.get(i) for i in bwolist]

    for bwobject in objlist:
        extracted_data = extract_data(bwobject)
        bwo_parent_list.append(extracted_data['bwparent'])
        logtext_list.append(extracted_data['logtext'])
        info_list.append(extracted_data['info'])
        w_metadata_list.append(extracted_data['w_metadata'])
        workflow_func_list.append(extracted_data['workflow_func'])
        if bwobject.get_action() not in actionlist:
            actionlist.append(bwobject.get_action())

    action_form = actions[actionlist[0]]

    result = action_form().render(objlist, bwo_parent_list, info_list,
                                  logtext_list, w_metadata_list,
                                  workflow_func_list)
    url, parameters = result

    return render_template(url, **parameters)


@blueprint.route('/get_version_showing', methods=['GET', 'POST'])
@login_required
def get_version_showing():
    """Return current version showing, from flask session."""
    try:
        return session['workflows_version_showing']
    except KeyError:
        return None


@blueprint.route('/details/<int:objectid>', methods=['GET', 'POST'])
@register_breadcrumb(blueprint, '.details', _("Record Details"))
@login_required
def details(objectid):
    """Display info about the object."""
    of = "hd"
    bwobject = BibWorkflowObject.query.get(objectid)
    from invenio.ext.sqlalchemy import db

    #formatted_data = bwobject.get_formatted_data(of)
    workflows_uuid = bwobject.id_workflow
    if workflows_uuid:
        formatted_data = workflows[Workflow.query.get(workflows_uuid).name]().formatter(bwobject, formatter=None, format=of)
    else:
        formatted_data = ""

    extracted_data = extract_data(bwobject)

    action_name = bwobject.get_action()
    if action_name:
        action = actions[action_name]
        rendered_actions = action().render(bwobject)
    else:
        rendered_actions = {}

    if bwobject.id_parent:
        hbwobject_db_request = BibWorkflowObject.query.filter(
            db.or_(BibWorkflowObject.id_parent == bwobject.id_parent,
                   BibWorkflowObject.id == bwobject.id_parent,
                   BibWorkflowObject.id == bwobject.id)).all()

    else:
        hbwobject_db_request = BibWorkflowObject.query.filter(
            db.or_(BibWorkflowObject.id_parent == bwobject.id,
                   BibWorkflowObject.id == bwobject.id)).all()

    hbwobject = {ObjectVersion.FINAL: [], ObjectVersion.HALTED: [],
                 ObjectVersion.INITIAL: [], ObjectVersion.RUNNING: []}

    for hbobject in hbwobject_db_request:
        hbwobject[hbobject.version].append({"id": hbobject.id,
                                            "version": hbobject.version,
                                            "date": pretty_date(hbobject.created),
                                            "true_date": hbobject.modified})

    for list_of_object in hbwobject:
        hbwobject[list_of_object].sort(key=lambda x: x["true_date"], reverse=True)

    hbwobject_final = hbwobject[ObjectVersion.INITIAL] + \
        hbwobject[ObjectVersion.HALTED] + \
        hbwobject[ObjectVersion.FINAL]

    results = []
    for label, res in bwobject.get_tasks_results().iteritems():
        res_dicts = [item.to_dict() for item in res]
        results.append((label, res_dicts))

    return render_template('workflows/hp_details.html',
                           bwobject=bwobject,
                           rendered_actions=rendered_actions,
                           hbwobject=hbwobject_final,
                           bwparent=extracted_data['bwparent'],
                           info=extracted_data['info'],
                           log=extracted_data['logtext'],
                           data_preview=formatted_data,
                           workflow_func=extracted_data['workflow_func'],
                           workflow=extracted_data['w_metadata'],
                           task_results=results)


@blueprint.route('/restart_record', methods=['GET', 'POST'])
@login_required
@wash_arguments({'objectid': (int, 0)})
def restart_record(objectid, start_point='continue_next'):
    """Restart the initial object in its workflow."""
    bwobject = BibWorkflowObject.query.get(objectid)

    workflow = Workflow.query.filter(
        Workflow.uuid == bwobject.id_workflow).first()

    start_delayed(workflow.name, [bwobject.get_data()])
    return 'Record Restarted'


@blueprint.route('/continue_record', methods=['GET', 'POST'])
@login_required
@wash_arguments({'objectid': (int, 0)})
def continue_record(objectid):
    """Continue workflow for current object."""
    continue_oid_delayed(oid=objectid, start_point='continue_next')
    return 'Record continued workflow'


@blueprint.route('/restart_record_prev', methods=['GET', 'POST'])
@login_required
@wash_arguments({'objectid': (int, 0)})
def restart_record_prev(objectid):
    """Restart the last task for current object."""
    continue_oid_delayed(oid=objectid, start_point="restart_task")
    return 'Record restarted current task'


@blueprint.route('/delete', methods=['GET', 'POST'])
@login_required
@wash_arguments({'objectid': (int, 0)})
def delete_from_db(objectid):
    """Delete the object from the db."""
    BibWorkflowObject.delete(objectid)
    return 'Record Deleted'


@blueprint.route('/delete_multi', methods=['GET', 'POST'])
@login_required
@wash_arguments({'bwolist': (text_type, "")})
def delete_multi(bwolist):
    """Delete list of objects from the db."""
    from ..utils import parse_bwids

    bwolist = parse_bwids(bwolist)
    for objectid in bwolist:
        delete_from_db(objectid)
    return 'Records Deleted'


@blueprint.route('/resolve', methods=['GET', 'POST'])
@login_required
@wash_arguments({'objectid': (text_type, '-1')})
def resolve_action(objectid):
    """Resolve the action taken.

    Will call the run() function of the specific action.
    """
    bwobject = BibWorkflowObject.query.get(int(objectid))
    action_name = bwobject.get_action()
    action_form = actions[action_name]
    res = action_form().resolve(bwobject)
    return jsonify(res)


@blueprint.route('/entry_data_preview', methods=['GET', 'POST'])
@login_required
@wash_arguments({'objectid': (text_type, '0'),
                 'of': (text_type, None)})
def entry_data_preview(objectid, of):
    """Present the data in a human readble form or in xml code."""
    from flask import Markup
    from pprint import pformat

    bwobject = BibWorkflowObject.query.get(int(objectid))

    if not bwobject:
        flash("No object found for %s" % (objectid,))
        return jsonify(data={})

    workflows_uuid = bwobject.id_workflow
    if workflows_uuid:
        formatted_data = workflows[Workflow.query.get(workflows_uuid).name]().formatter(bwobject, formatter=None, format=of)
    else:
        formatted_data = ""
    if isinstance(formatted_data, dict):
        formatted_data = pformat(formatted_data)
    if of and of in ("xm", "xml", "marcxml"):
        data = Markup.escape(formatted_data)
    else:
        data = formatted_data
    return jsonify(data=data)


@blueprint.route('/get_context', methods=['GET', 'POST'])
@login_required
def get_context():
    """Return the a JSON structure with URL maps and actions."""
    context = {}
    context['url_prefix'] = blueprint.url_prefix
    context['holdingpen'] = {
        "url_load": url_for('holdingpen.load_table'),
        "url_preview": url_for('holdingpen.entry_data_preview'),
        "url_restart_record": url_for('holdingpen.restart_record'),
        "url_restart_record_prev": url_for('holdingpen.restart_record_prev'),
        "url_continue_record": url_for('holdingpen.continue_record'),
    }
    try:
        context['version_showing'] = session['workflows_version_showing']
    except KeyError:
        context['version_showing'] = ObjectVersion.HALTED

    return jsonify(context)


def get_info(bwobject):
    """Parses the hpobject and extracts its info to a dictionary."""
    info = {}
    if bwobject.get_extra_data()['owner'] != {}:
        info['owner'] = bwobject.get_extra_data()['owner']
    else:
        info['owner'] = 'None'
    info['parent id'] = bwobject.id_parent
    info['workflow id'] = bwobject.id_workflow
    info['object id'] = bwobject.id
    info['action'] = bwobject.get_action()
    return info


def extract_data(bwobject):
    """Extract needed metadata from BibWorkflowObject.

    Used for rendering the Record's holdingpen table row and
    details and action page.
    """
    extracted_data = {}
    if bwobject.id_parent is not None:
        extracted_data['bwparent'] = \
            BibWorkflowObject.query.get(bwobject.id_parent)
    else:
        extracted_data['bwparent'] = None

    # TODO: read the logstuff from the db
    extracted_data['loginfo'] = ""
    extracted_data['logtext'] = {}

    for log in extracted_data['loginfo']:
        extracted_data['logtext'][log.get_extra_data()['_last_task_name']] = \
            log.message

    extracted_data['info'] = get_info(bwobject)
    try:
        extracted_data['info']['action'] = bwobject.get_action()
    except (KeyError, AttributeError):
        pass

    extracted_data['w_metadata'] = \
        Workflow.query.filter(Workflow.uuid == bwobject.id_workflow).first()
    if extracted_data['w_metadata']:
        workflow_def = get_workflow_definition(extracted_data['w_metadata'].name)
        extracted_data['workflow_func'] = workflow_def
    else:
        extracted_data['workflow_func'] = [None]
    return extracted_data


def get_action_list(object_list):
    """Return a dict of action names mapped to halted objects.

    Get a dictionary mapping from action name to number of Pending
    actions (i.e. halted objects). Used in the holdingpen.index page.
    """
    action_dict = {}
    found_actions = []

    # First get a list of all to count up later
    for bwo in object_list:
        action_name = bwo.get_action()
        if action_name is not None:
            found_actions.append(action_name)

    # Get "real" action name only once per action
    for action_name in set(found_actions):
        if action_name not in actions:
            # Perhaps some old action? Use stored name.
            action_nicename = action_name
        else:
            action = actions[action_name]
            action_nicename = getattr(action, "name", action_name)
        action_dict[action_nicename] = found_actions.count(action_name)
    return action_dict


@blueprint.route('/load_table', methods=['GET', 'POST'])
@login_required
@templated('workflows/hp_maintable.html')
def load_table():
    """Get JSON data for the Holdingpen table.

    Function used for the passing of JSON data to the DataTable
    1] First checks for what record version to show
    2] then sorting direction,
    3] then if the user searched for something
    and finally it builds the JSON to send.
    """
    import time
    StartA = time.time()
    try:
        workflows_cache = {}
        if request.method == "POST":
            if "holdingpen_tags" not in session:
                session["holdingpen_tags"] = []
            if request.json and "tags" in request.json:
                tags = request.json["tags"]
                session["holdingpen_tags"] = tags
            elif "holdingpen_tags" in session:
                tags = session["holdingpen_tags"]
            else:
                tags = []
                session["holdingpen_tags"] = []
                print "global request  {0}".format(time.time() - StartA)
            return None
        else:
            if "holdingpen_tags" in session:
                tags = session["holdingpen_tags"]
            else:
                tags = []
                session["holdingpen_tags"] = []

        i_sortcol_0 = request.args.get('iSortCol_0', session.get('iSortCol_0', 0))
        s_sortdir_0 = request.args.get('sSortDir_0', session.get('sSortDir_0', None))

        session["holdingpen_iDisplayStart"] = int(request.args.get('iDisplayStart', session.get('iDisplayLength', 10)))
        session["holdingpen_iDisplayLength"] = int(request.args.get('iDisplayLength', session.get('iDisplayLength', 0)))
        session["holdingpen_sEcho"] = int(request.args.get('sEcho', session.get('sEcho', 0))) + 1
        bwolist = get_holdingpen_objects(tags)
        print "get holdingpen object {0}".format(time.time() - StartA)
        if 'iSortCol_0' in session and "sSortDir_0" in session:
            i_sortcol_0 = int(str(i_sortcol_0))
            if i_sortcol_0 != session['iSortCol_0'] or s_sortdir_0 != session['sSortDir_0']:
                bwolist = sort_bwolist(bwolist, i_sortcol_0, s_sortdir_0)
        session["holdingpen_iSortCol_0"] = i_sortcol_0
        session["holdingpen_sSortDir_0"] = s_sortdir_0

        table_data = {'aaData': [],
                      'iTotalRecords': len(bwolist),
                      'iTotalDisplayRecords': len(bwolist),
                      'sEcho': session["holdingpen_sEcho"]}

        # This will be simplified once Redis is utilized.
        records_showing = 0
        for bwo in bwolist[session["holdingpen_iDisplayStart"]:session["holdingpen_iDisplayStart"] + session["holdingpen_iDisplayLength"]]:
            action_name = bwo.get_action()
            action_message = bwo.get_action_message()
            if not action_message:
                action_message = ""

            action = actions.get(action_name, None)
            records_showing += 1
            workflows_uuid = bwo.id_workflow
            if workflows_uuid and workflows_uuid not in workflows_cache:
                workflows_cache[workflows_uuid] = workflows[Workflow.query.get(workflows_uuid).name]()
            mini_action = getattr(action, "mini_action", None)
            record = bwo.get_data()
            if not hasattr(record, "get"):
                try:
                    record = dict(record)
                except:
                    record = {}
            if not workflows_uuid:
                title = "No title"
                description = "No description"
                extra_data = bwo.get_extra_data()
            else:
                title = workflows_cache[workflows_uuid].get_title(bwo)
                description = workflows_cache[workflows_uuid].get_description(bwo)
                extra_data = bwo.get_extra_data()
            row = render_template('workflows/row_formatter.html',
                                  title=title,
                                  object=bwo,
                                  record=record,
                                  extra_data=extra_data,
                                  description=description,
                                  action=action,
                                  mini_action=mini_action,
                                  action_message=action_message,
                                  pretty_date=pretty_date,
                                  )
            d = {}
            for key, value in REG_TD.findall(row):
                d[key] = value.strip()

            table_data['aaData'].append(
                [d['id'],
                 d['checkbox'],
                 d['title'],
                 d['description'],
                 d['pretty_date'],
                 d['version'],
                 d['type'],
                 d['action']]
            )
        print "global request  {0}".format(time.time() - StartA)
        return jsonify(table_data)
    except Exception as e:
        print e


def get_holdingpen_objects(ptags=[]):
    """Get BibWorkflowObject's for display in Holding Pen.

    Uses DataTable naming for filtering/sorting. Work in progress.
    """
    import time
    workflows_cache = {}
    tags = ptags[:]
    temp_list = ["New", "Done", "Need action", "In process"]
    version_showing = []
    for i in range(len(tags) - 1, -1, -1):
        if tags[i] in temp_list:
            version_showing.append(ObjectVersion.REVERSE_MAPPING[tags[i]])
            del tags[i]

    if version_showing is None:
        version_showing = ObjectVersion.MAPPING.keys()
    ssearch = tags
    bwobject_list = BibWorkflowObject.query.filter(
        BibWorkflowObject.id_parent == None
    ).filter(not version_showing or BibWorkflowObject.version.in_(
        version_showing)).all()

    if ssearch:
        if not isinstance(ssearch, list):
            if "," in ssearch:
                ssearch = ssearch.split(",")
            else:
                ssearch = [ssearch]

        bwobject_list_tmp = []
        for bwo in bwobject_list:
            startA = time.time()
            workflows_uuid = bwo.id_workflow
            if workflows_uuid and workflows_uuid not in workflows_cache:
                workflows_cache[workflows_uuid] = workflows[Workflow.query.get(workflows_uuid).name]()

            if workflows_uuid:
                checking_functions = {"title": workflows_cache[workflows_uuid].get_title,
                                      "description": workflows_cache[workflows_uuid].get_description,
                                      "created": get_pretty_date,
                                      "type": get_type
                                      }
            else:
                checking_functions = {"title": WorkflowBase.get_title,
                                      "description": WorkflowBase.get_description,
                                      "created": get_pretty_date,
                                      "type": get_type
                                      }
            startB = time.time()
            for function in checking_functions:
                if check_ssearch_over_data(ssearch, checking_functions[function](bwo)):
                    bwobject_list_tmp.append(bwo)
                    break
            else:
                print "A {0}, B {1}, ratio {2}".format(time.time() - startA,time.time() - startB,(time.time() - startB)/(time.time() - startA))
                break  # executed if the loop ended normally (no break)
            print "A {0}, B {1}, ratio {2}".format(time.time() - startA,time.time() - startB,(time.time() - startB)/(time.time() - startA))
            continue

        bwobject_list = bwobject_list_tmp
    return bwobject_list


def check_ssearch_over_data(ssearch, data):
    """
    Check if the data match with one of the search tag in data.

    :param ssearch: list of tags used for filtering.
    :param data: data to check.
    :return: True if present False otherwise.
    """
    if not isinstance(ssearch, list):
        if "," in ssearch:
            ssearch = ssearch.split(",")
        else:
            ssearch = [ssearch]
    if not isinstance(data, list):
        data = [data]

    result = True

    for terms in ssearch:
        for datum in data:
            if terms.lower() not in datum.lower():
                result = False
                break
    return result


def get_pretty_date(bwo):
    return pretty_date(bwo.created)


def get_type(bwo):
    """Get the type of the Object."""
    return bwo.data_type
