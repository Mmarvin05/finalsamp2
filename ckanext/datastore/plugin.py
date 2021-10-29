# encoding: utf-8

import logging
import os
import ckan.plugins as p
import ckan.logic as logic
from ckan.model.core import State
from ckan.config.declaration import Declaration, Key

import ckanext.datastore.helpers as datastore_helpers
import ckanext.datastore.logic.action as action
import ckanext.datastore.logic.auth as auth
import ckanext.datastore.interfaces as interfaces
from ckanext.datastore.backend import (
    DatastoreException,
    _parse_sort_clause,
    DatastoreBackend
)
from ckanext.datastore.backend.postgres import DatastorePostgresqlBackend
import ckanext.datastore.blueprint as view

log = logging.getLogger(__name__)
_get_or_bust = logic.get_or_bust

_SQL_FUNCTIONS_ALLOWLIST_FILE = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "allowed_functions.txt"
)

DEFAULT_FORMATS = []

ValidationError = p.toolkit.ValidationError


class DatastorePlugin(p.SingletonPlugin):
    p.implements(p.IConfigurable, inherit=True)
    p.implements(p.IConfigDeclaration)
    p.implements(p.IConfigurer)
    p.implements(p.IActions)
    p.implements(p.IAuthFunctions)
    p.implements(p.IRoutes, inherit=True)
    p.implements(p.IResourceController, inherit=True)
    p.implements(p.ITemplateHelpers)
    p.implements(p.IForkObserver, inherit=True)
    p.implements(interfaces.IDatastore, inherit=True)
    p.implements(interfaces.IDatastoreBackend, inherit=True)
    p.implements(p.IBlueprint)

    resource_show_action = None

    def __new__(cls, *args, **kwargs):
        idatastore_extensions = p.PluginImplementations(interfaces.IDatastore)
        idatastore_extensions = idatastore_extensions.extensions()

        if idatastore_extensions and idatastore_extensions[0].__class__ != cls:
            msg = ('The "datastore" plugin must be the first IDatastore '
                   'plugin loaded. Change the order it is loaded in '
                   '"ckan.plugins" in your CKAN .ini file and try again.')
            raise DatastoreException(msg)

        return super(cls, cls).__new__(cls, *args, **kwargs)

    # IDatastoreBackend

    def register_backends(self):
        return {
            'postgresql': DatastorePostgresqlBackend,
            'postgres': DatastorePostgresqlBackend,
        }

    # IConfigurer

    def update_config(self, config):
        DatastoreBackend.register_backends()
        DatastoreBackend.set_active_backend(config)

        templates_base = config.get_value('ckan.base_templates_folder')

        p.toolkit.add_template_directory(config, templates_base)
        self.backend = DatastoreBackend.get_active_backend()

    # IConfigurable

    def configure(self, config):
        self.config = config
        self.backend.configure(config)

    # IConfigDeclaration

    def declare_config_options(self, declaration: Declaration, key: Key):
        section = key.ckan.datastore

        declaration.annotate("Datastore settings")
        declaration.declare(
            section.write_url,
            "postgresql://ckan_default:pass@localhost/datastore_default"
        ).required()
        declaration.declare(
            section.read_url,
            "postgresql://datastore_default:pass@localhost/datastore_default"
        ).required()

        declaration.declare(
            section.sqlsearch.allowed_functions_file,
            _SQL_FUNCTIONS_ALLOWLIST_FILE)
        declaration.declare_bool(section.sqlsearch.enabled, False)
        declaration.declare_int(section.search.rows_default, 100)
        declaration.declare_int(section.search.rows_max, 32000)
        declaration.declare_dynamic(section.sqlalchemy.dynamic("OPTION"))
#
        declaration.annotate("PostgreSQL' full-text search parameters")
        declaration.declare(section.default_fts_lang, "english")
        declaration.declare(section.default_fts_index_method, "gist")

    # IActions

    def get_actions(self):
        actions = {
            'datastore_create': action.datastore_create,
            'datastore_upsert': action.datastore_upsert,
            'datastore_delete': action.datastore_delete,
            'datastore_search': action.datastore_search,
            'datastore_info': action.datastore_info,
            'datastore_function_create': action.datastore_function_create,
            'datastore_function_delete': action.datastore_function_delete,
            'datastore_run_triggers': action.datastore_run_triggers,
        }
        if getattr(self.backend, 'enable_sql_search', False):
            # Only enable search_sql if the config/backend does not disable it
            actions.update({
                'datastore_search_sql': action.datastore_search_sql,
            })
        return actions

    # IAuthFunctions

    def get_auth_functions(self):
        return {
            'datastore_create': auth.datastore_create,
            'datastore_upsert': auth.datastore_upsert,
            'datastore_delete': auth.datastore_delete,
            'datastore_info': auth.datastore_info,
            'datastore_search': auth.datastore_search,
            'datastore_search_sql': auth.datastore_search_sql,
            'datastore_change_permissions': auth.datastore_change_permissions,
            'datastore_function_create': auth.datastore_function_create,
            'datastore_function_delete': auth.datastore_function_delete,
            'datastore_run_triggers': auth.datastore_run_triggers,
        }

    # IResourceController

    def before_resource_show(self, resource_dict):
        # Modify the resource url of datastore resources so that
        # they link to the datastore dumps.
        if resource_dict.get('url_type') == 'datastore':
            resource_dict['url'] = p.toolkit.url_for(
                'datastore.dump', resource_id=resource_dict['id'],
                qualified=True)

        if 'datastore_active' not in resource_dict:
            resource_dict[u'datastore_active'] = False

        return resource_dict

    def after_resource_delete(self, context, resources):
        model = context['model']
        pkg = context['package']
        res_query = model.Session.query(model.Resource)
        query = res_query.filter(
            model.Resource.package_id == pkg.id,
            model.Resource.state == State.DELETED
        )
        deleted = [
            res for res in query.all()
            if res.extras.get('datastore_active') is True]

        for res in deleted:
            if self.backend.resource_exists(res.id):
                self.backend.delete(context, {
                    'resource_id': res.id,
                })
            res.extras['datastore_active'] = False
            res_query.filter_by(id=res.id).update(
                {'extras': res.extras}, synchronize_session=False)

    # IDatastore

    def datastore_validate(self, context, data_dict, fields_types):
        column_names = list(fields_types.keys())

        filters = data_dict.get('filters', {})
        for key in list(filters.keys()):
            if key in fields_types:
                del filters[key]

        q = data_dict.get('q')
        if q:
            if isinstance(q, str):
                del data_dict['q']
                column_names.append(u'rank')
            elif isinstance(q, dict):
                for key in list(q.keys()):
                    if key in fields_types and isinstance(q[key],
                                                          str):
                        column_names.append(u'rank ' + key)
                        del q[key]

        fields = data_dict.get('fields')
        if fields:
            data_dict['fields'] = list(set(fields) - set(column_names))

        language = data_dict.get('language')
        if language:
            if isinstance(language, str):
                del data_dict['language']

        plain = data_dict.get('plain')
        if plain:
            if isinstance(plain, bool):
                del data_dict['plain']

        distinct = data_dict.get('distinct')
        if distinct:
            if isinstance(distinct, bool):
                del data_dict['distinct']

        sort_clauses = data_dict.get('sort')
        if sort_clauses:
            invalid_clauses = [
                c for c in sort_clauses
                if not _parse_sort_clause(
                    c, fields_types
                )
            ]
            data_dict['sort'] = invalid_clauses

        limit = data_dict.get('limit')
        if limit:
            is_positive_int = datastore_helpers.validate_int(limit,
                                                             non_negative=True)
            is_all = isinstance(limit, str) and limit.lower() == 'all'
            if is_positive_int or is_all:
                del data_dict['limit']

        offset = data_dict.get('offset')
        if offset:
            is_positive_int = datastore_helpers.validate_int(offset,
                                                             non_negative=True)
            if is_positive_int:
                del data_dict['offset']

        full_text = data_dict.get('full_text')
        if full_text:
            if isinstance(full_text, str):
                del data_dict['full_text']

        return data_dict

    def datastore_delete(self, context, data_dict, fields_types, query_dict):
        hook = getattr(self.backend, 'datastore_delete', None)
        if hook:
            query_dict = hook(context, data_dict, fields_types, query_dict)
        return query_dict

    def datastore_search(self, context, data_dict, fields_types, query_dict):
        hook = getattr(self.backend, 'datastore_search', None)
        if hook:
            query_dict = hook(context, data_dict, fields_types, query_dict)
        return query_dict

    def get_helpers(self):
        return {
            'datastore_dictionary': datastore_helpers.datastore_dictionary}

    # IForkObserver

    def before_fork(self):
        try:
            before_fork = self.backend.before_fork
        except AttributeError:
            pass
        else:
            before_fork()

    # IBlueprint

    def get_blueprint(self):
        u'''Return a Flask Blueprint object to be registered by the app.'''

        return view.datastore
