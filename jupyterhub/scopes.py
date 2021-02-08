import functools
import inspect
from enum import Enum

from tornado import web
from tornado.log import app_log

from . import orm


class Scope(Enum):
    ALL = True


def get_user_scopes(name):
    """
    Scopes have a metascope 'all' that should be expanded to everything a user can do.
    At the moment that is a user-filtered version (optional read) access to
    users
    users:name
    users:groups
    users:activity
    users:servers
    users:tokens
    """
    scope_list = [
        'users',
        'users:name',
        'users:groups',
        'users:activity',
        'users:servers',
        'users:tokens',
    ]
    scope_list.extend(['read:' + scope for scope in scope_list])
    return {"{}!user={}".format(scope, name) for scope in scope_list}


def _needs_scope_expansion(filter_, filter_value, sub_scope):
    """
    Check if there is a requirements to expand the `group` scope to individual `user` scopes.
    Assumptions:
    filter_ != Scope.ALL
    """
    if not (filter_ == 'user' and 'group' in sub_scope):
        return False
    if 'user' in sub_scope:
        return filter_value not in sub_scope['user']
    else:
        return True


def _check_user_in_expanded_scope(handler, user_name, scope_group_names):
    """Check if username is present in set of allowed groups"""
    user = handler.find_user(user_name)
    if user is None:
        raise web.HTTPError(404, "No access to resources or resources not found")
    group_names = {group.name for group in user.groups}  # SQL query faster?
    return bool(set(scope_group_names) & group_names)


def _get_scope_filter(db, req_scope, sub_scope):
    """Produce a filter for `*ListAPIHandlers* so that get method knows which models to return"""
    scope_translator = {
        'read:users': 'users',
        'read:services': 'services',
        'read:groups': 'groups',
    }
    if req_scope not in scope_translator:
        raise AttributeError("Internal error: inconsistent scope situation")
    kind = scope_translator[req_scope]
    Resource = orm.get_class(kind)
    sub_scope_values = next(iter(sub_scope.values()))
    query = db.query(Resource).filter(Resource.name.in_(sub_scope_values))
    scope_filter = {entry.name for entry in query.all()}
    if 'group' in sub_scope and kind == 'users':
        groups = orm.Group.name.in_(sub_scope['group'])
        users_in_groups = db.query(orm.User).join(orm.Group.users).filter(groups)
        scope_filter |= {user.name for user in users_in_groups}
    return scope_filter


def _check_scope(api_handler, req_scope, **kwargs):
    """Check if scopes satisfy requirements
    Returns either Scope.ALL for unrestricted access, Scope.NONE for refused access or
    an iterable with a filter
    """
    # Parse user name and server name together
    if 'user' in kwargs and 'server' in kwargs:
        kwargs['server'] = "{}/{}".format(kwargs['user'], kwargs['server'])
    if req_scope not in api_handler.parsed_scopes:
        return False
    if api_handler.parsed_scopes[req_scope] == Scope.ALL:
        return True
    # Apply filters
    sub_scope = api_handler.parsed_scopes[req_scope]
    if 'scope_filter' in kwargs:
        scope_filter = _get_scope_filter(api_handler.db, req_scope, sub_scope)
        return scope_filter
    else:
        if not kwargs:
            return False  # Separated from 404 error below because in this case we don't leak information
        # Interface change: Now can have multiple filters
        for (filter_, filter_value) in kwargs.items():
            if filter_ in sub_scope and filter_value in sub_scope[filter_]:
                return True
            if _needs_scope_expansion(filter_, filter_value, sub_scope):
                group_names = sub_scope['group']
                if _check_user_in_expanded_scope(
                    api_handler, filter_value, group_names
                ):
                    return True
        raise web.HTTPError(404, "No access to resources or resources not found")


def parse_scopes(scope_list):
    """
    Parses scopes and filters in something akin to JSON style

    For instance, scope list ["users", "groups!group=foo", "users:servers!server=bar", "users:servers!server=baz"]
    would lead to scope model
    {
       "users":scope.ALL,
       "users:admin":{
          "user":[
             "alice"
          ]
       },
       "users:servers":{
          "server":[
             "bar",
             "baz"
          ]
       }
    }
    """
    parsed_scopes = {}
    for scope in scope_list:
        base_scope, _, filter_ = scope.partition('!')
        if not filter_:
            parsed_scopes[base_scope] = Scope.ALL
        elif base_scope not in parsed_scopes:
            parsed_scopes[base_scope] = {}
        if parsed_scopes[base_scope] != Scope.ALL:
            key, _, val = filter_.partition('=')
            if key not in parsed_scopes[base_scope]:
                parsed_scopes[base_scope][key] = []
            parsed_scopes[base_scope][key].append(val)
    return parsed_scopes


def needs_scope(scope):
    """Decorator to restrict access to users or services with the required scope"""

    def scope_decorator(func):
        @functools.wraps(func)
        def _auth_func(self, *args, **kwargs):
            sig = inspect.signature(func)
            bound_sig = sig.bind(self, *args, **kwargs)
            bound_sig.apply_defaults()
            s_kwargs = {}
            for resource in {'user', 'server', 'group', 'service'}:
                resource_name = resource + '_name'
                if resource_name in bound_sig.arguments:
                    resource_value = bound_sig.arguments[resource_name]
                    s_kwargs[resource] = resource_value
            if 'scope_filter' in bound_sig.arguments:
                s_kwargs['scope_filter'] = None
            scope_filter = _check_scope(self, scope, **s_kwargs)
            # todo: This checks if True/False or set of resource names. Can be improved
            if isinstance(scope_filter, set):
                kwargs['scope_filter'] = scope_filter
            if scope_filter:
                return func(self, *args, **kwargs)
            else:
                # catching attr error occurring for older_requirements test
                # could be done more elegantly?
                try:
                    request_path = self.request.path
                except AttributeError:
                    request_path = 'the requested API'
                app_log.warning(
                    "Not authorizing access to {}. Requires scope {}, not derived from scopes [{}]".format(
                        request_path, scope, ", ".join(self.raw_scopes)
                    )
                )
                raise web.HTTPError(
                    403,
                    "Action is not authorized with current scopes; requires {}".format(
                        scope
                    ),
                )

        return _auth_func

    return scope_decorator
