import inspect
import re
import typing
from collections import OrderedDict
from decimal import Decimal
from operator import attrgetter

import uritemplate
from django.core import validators, exceptions as django_exceptions
from django.db import models
from rest_framework import permissions, renderers, serializers
from rest_framework.fields import _UnvalidatedField, empty
from rest_framework.generics import GenericAPIView
from rest_framework.mixins import ListModelMixin
from rest_framework.schemas.inspectors import ViewInspector
from rest_framework.schemas.utils import get_pk_description  # type: ignore
from rest_framework.settings import api_settings
from rest_framework.utils.model_meta import get_field_info
from rest_framework.views import APIView

from drf_spectacular.extensions import (
    OpenApiSerializerFieldExtension, OpenApiSerializerExtension
)
from drf_spectacular.settings import spectacular_settings
from drf_spectacular.contrib.authentication import *  # noqa: F403, F401
from drf_spectacular.contrib.serializers import *  # noqa: F403, F401
from drf_spectacular.plumbing import (
    build_basic_type, warn, anyisinstance, force_instance, is_serializer,
    follow_field_source, is_field, is_basic_type, build_array_type,
    ComponentRegistry, ResolvedComponent, build_parameter_type, error,
    resolve_regex_path_parameter, safe_ref, has_override, get_override,
    append_meta, build_object_type,
)
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter
from drf_spectacular.authentication import OpenApiAuthenticationExtension


class AutoSchema(ViewInspector):
    method_mapping = {
        'get': 'retrieve',
        'post': 'create',
        'put': 'update',
        'patch': 'partial_update',
        'delete': 'destroy',
    }

    def get_operation(self, path, path_regex, method, registry: ComponentRegistry):
        self.registry = registry
        self.path = path
        self.path_regex = path_regex
        self.method = method

        operation = {}

        operation['operationId'] = self.get_operation_id()
        operation['description'] = self.get_description()

        parameters = self._get_parameters()
        if parameters:
            operation['parameters'] = parameters

        tags = self.get_tags()
        if tags:
            operation['tags'] = tags

        request_body = self._get_request_body()
        if request_body:
            operation['requestBody'] = request_body

        auth = self.get_auth()
        if auth:
            operation['security'] = auth

        deprecated = self.is_deprecated()
        if deprecated:
            operation['deprecated'] = deprecated

        operation['responses'] = self._get_response_bodies()

        return operation

    def _is_list_view(self, serializer=None):
        """
        partially heuristic approach to determine if a view yields an object or a
        list of objects. used for operationId naming, array building and pagination.
        defaults to False if all introspection fail.
        """
        if serializer is None:
            serializer = self.get_response_serializers()

        if isinstance(serializer, dict) and serializer:
            # extract likely main serializer from @extend_schema override
            serializer = {str(code): s for code, s in serializer.items()}
            serializer = serializer[min(serializer)]

        if isinstance(serializer, serializers.ListSerializer):
            return True
        if is_basic_type(serializer):
            return False
        if hasattr(self.view, 'action'):
            return self.view.action == 'list'
        # list responses are "usually" only returned by GET
        if self.method.lower() != 'get':
            return False
        if isinstance(self.view, ListModelMixin):
            return True
        # primary key/lookup variable in path is a strong indicator for retrieve
        if isinstance(self.view, GenericAPIView):
            lookup_url_kwarg = self.view.lookup_url_kwarg or self.view.lookup_field
            if lookup_url_kwarg in uritemplate.variables(self.path):
                return False

        return False

    def get_override_parameters(self):
        """ override this for custom behaviour """
        return []

    def _process_override_parameters(self):
        result = []
        for parameter in self.get_override_parameters():
            if isinstance(parameter, OpenApiParameter):
                if is_basic_type(parameter.type):
                    schema = build_basic_type(parameter.type)
                elif is_serializer(parameter.type):
                    schema = self.resolve_serializer(parameter.type, 'request').ref
                else:
                    schema = parameter.type
                result.append(build_parameter_type(
                    name=parameter.name,
                    schema=schema,
                    location=parameter.location,
                    required=parameter.required,
                    description=parameter.description,
                    enum=parameter.enum,
                    deprecated=parameter.deprecated,
                ))
            elif is_serializer(parameter):
                # explode serializer into separate parameters. defaults to QUERY location
                mapped = self._map_serializer(parameter, 'request')
                for property_name, property_schema in mapped['properties'].items():
                    result.append(build_parameter_type(
                        name=property_name,
                        schema=property_schema,
                        location=OpenApiParameter.QUERY,
                        required=property_name in mapped.get('required', [])
                    ))
            else:
                warn(f'could not resolve parameter annotation {parameter}. skipping.')
        return result

    def _get_parameters(self):
        def dict_helper(parameters):
            return {(p['name'], p['in']): p for p in parameters}

        override_parameters = dict_helper(self._process_override_parameters())
        # remove overridden path parameters beforehand so that there are no irrelevant warnings.
        path_variables = [
            v for v in uritemplate.variables(self.path) if (v, 'path') not in override_parameters
        ]
        parameters = {
            **dict_helper(self._resolve_path_parameters(path_variables)),
            **dict_helper(self._get_filter_parameters()),
            **dict_helper(self._get_pagination_parameters()),
        }
        # override/add @extend_schema parameters
        for key, parameter in override_parameters.items():
            parameters[key] = parameter
        return sorted(parameters.values(), key=lambda p: p['name'])

    def get_description(self):
        """ override this for custom behaviour """
        action_or_method = getattr(self.view, getattr(self.view, 'action', self.method.lower()), None)
        view_doc = inspect.getdoc(self.view) or ''
        action_doc = inspect.getdoc(action_or_method) or ''
        return action_doc or view_doc

    def get_auth(self):
        """
        Obtains authentication classes and permissions from view. If authentication
        is known, resolve security requirement for endpoint and security definition for
        the component section.
        For custom authentication subclass ``OpenApiAuthenticationExtension``.
        """
        auths = []

        for authenticator in self.view.get_authenticators():
            scheme = OpenApiAuthenticationExtension.get_match(authenticator)
            if not scheme:
                warn(
                    f'could not resolve authenticator {authenticator.__class__}. There '
                    f'was no OpenApiAuthenticationExtension registered for that class. '
                    f'Try creating one by subclassing it. Ignoring for now.'
                )
                continue

            security_requirements = scheme.get_security_requirement(self)
            if security_requirements is not None:
                auths.append(security_requirements)

            component = ResolvedComponent(
                name=scheme.name,
                type=ResolvedComponent.SECURITY_SCHEMA,
                object=authenticator.__class__,
                schema=scheme.get_security_definition(self)
            )
            if component not in self.registry:
                self.registry.register(component)

        perms = [p.__class__ for p in self.view.get_permissions()]
        if permissions.AllowAny in perms:
            auths.append({})
        elif permissions.IsAuthenticatedOrReadOnly in perms and self.method not in ('PUT', 'PATCH', 'POST'):
            auths.append({})
        return auths

    def get_request_serializer(self):
        """ override this for custom behaviour """
        return self._get_serializer()

    def get_response_serializers(self):
        """ override this for custom behaviour """
        return self._get_serializer()

    def get_tags(self) -> typing.List[str]:
        """ override this for custom behaviour """
        tokenized_path = self._tokenize_path()
        # use first non-parameter path part as tag
        return tokenized_path[:1]

    def get_operation_id(self):
        """ override this for custom behaviour """
        tokenized_path = self._tokenize_path()
        # replace dashes as they can be problematic later in code generation
        tokenized_path = [t.replace('-', '_') for t in tokenized_path]

        if self.method.lower() == 'get' and self._is_list_view():
            action = 'list'
        else:
            action = self.method_mapping[self.method.lower()]

        return '_'.join(tokenized_path + [action])

    def is_deprecated(self):
        """ override this for custom behaviour """
        return False

    def _tokenize_path(self):
        # remove path prefix
        path = re.sub(
            pattern=spectacular_settings.SCHEMA_PATH_PREFIX,
            repl='',
            string=self.path,
            flags=re.IGNORECASE
        )
        # cleanup and tokenize remaining parts.
        path = path.rstrip('/').lstrip('/').split('/')
        # remove path variables and empty tokens
        return [t for t in path if t and not t.startswith('{')]

    def _resolve_path_parameters(self, variables):
        model = getattr(getattr(self.view, 'queryset', None), 'model', None)
        parameters = []

        for idx, variable in enumerate(variables):
            schema = build_basic_type(OpenApiTypes.STR)
            description = ''
            required = True

            resolved_parameter = resolve_regex_path_parameter(self.path_regex, variable)

            if resolved_parameter:
                schema, required = resolved_parameter['schema'], resolved_parameter['required']
            elif not model:
                warn(
                    f'could not derive type of path parameter "{variable}" because '
                    f'{self.view.__class__} has no queryset. consider annotating the '
                    f'parameter type with @extend_schema. defaulting to "string".'
                )
            else:
                try:
                    model_field = model._meta.get_field(variable)
                    schema = self._map_model_field(model_field, direction=None)
                    # strip irrelevant meta data
                    irrelevant_field_meta = ['readOnly', 'writeOnly', 'nullable', 'default']
                    schema = {k: v for k, v in schema.items() if k not in irrelevant_field_meta}
                    if 'description' not in schema and model_field.primary_key:
                        description = get_pk_description(model, model_field)
                except django_exceptions.FieldDoesNotExist:
                    warn(
                        f'could not derive type of path parameter "{variable}" because '
                        f'model "{model}" did contain no such field. consider annotating '
                        f'parameter with @extend_schema. defaulting to "string".'
                    )

            parameters.append({
                "name": variable,
                "in": "path",
                "required": required,
                "description": description,
                'schema': schema,
            })

        return parameters

    def _get_filter_parameters(self):
        if not self._allows_filters():
            return []
        parameters = []
        for filter_backend in self.view.filter_backends:
            parameters += filter_backend().get_schema_operation_parameters(self.view)
        return parameters

    def _allows_filters(self):
        """
        Determine whether to include filter Fields in schema.

        Default implementation looks for ModelViewSet or GenericAPIView
        actions/methods that cause filtering on the default implementation.
        """
        if getattr(self.view, 'filter_backends', None) is None:
            return False
        if hasattr(self.view, 'action'):
            return self.view.action in ["list", "retrieve", "update", "partial_update", "destroy"]
        return self.method.lower() in ["get", "put", "patch", "delete"]

    def _get_pagination_parameters(self):
        view = self.view

        if not self._is_list_view():
            return []

        paginator = self._get_paginator()
        if not paginator:
            return []

        return paginator.get_schema_operation_parameters(view)

    def _map_choicefield(self, field):
        choices = list(OrderedDict.fromkeys(field.choices))  # preserve order and remove duplicates
        if all(isinstance(choice, bool) for choice in choices):
            type = 'boolean'
        elif all(isinstance(choice, int) for choice in choices):
            type = 'integer'
        elif all(isinstance(choice, (int, float, Decimal)) for choice in choices):  # `number` includes `integer`
            # Ref: https://tools.ietf.org/html/draft-wright-json-schema-validation-00#section-5.21
            type = 'number'
        elif all(isinstance(choice, str) for choice in choices):
            type = 'string'
        else:
            type = None

        mapping = {
            # The value of `enum` keyword MUST be an array and SHOULD be unique.
            # Ref: https://tools.ietf.org/html/draft-wright-json-schema-validation-00#section-5.20
            'enum': choices
        }

        # If We figured out `type` then and only then we should set it. It must be a string.
        # Ref: https://swagger.io/docs/specification/data-models/data-types/#mixed-type
        # It is optional but it can not be null.
        # Ref: https://tools.ietf.org/html/draft-wright-json-schema-validation-00#section-5.21
        if type:
            mapping['type'] = type
        return mapping

    def _map_model_field(self, model_field, direction):
        assert isinstance(model_field, models.Field)
        # to get a fully initialized serializer field we use DRF's own init logic
        try:
            field_cls, field_kwargs = serializers.ModelSerializer().build_field(
                field_name=model_field.name,
                info=get_field_info(model_field.model),
                model_class=model_field.model,
                nested_depth=0,
            )
            field = field_cls(**field_kwargs)
        except:  # noqa
            field = None

        # For some cases, the DRF init logic either breaks (custom field with internal type) or
        # the resulting field is underspecified with regards to the schema (ReadOnlyField).
        if field and not anyisinstance(field, [serializers.ReadOnlyField, serializers.ModelField]):
            return self._map_serializer_field(field, direction)
        elif isinstance(model_field, models.ForeignKey):
            return self._map_model_field(model_field.target_field, direction)
        elif hasattr(models, model_field.get_internal_type()):
            # be graceful when the model field is not explicitly mapped to a serializer
            internal_type = getattr(models, model_field.get_internal_type())
            field_cls = serializers.ModelSerializer.serializer_field_mapping[internal_type]
            return self._map_serializer_field(field_cls(), direction)
        else:
            error(
                f'could not resolve model field "{model_field}". failed to resolve through '
                f'serializer_field_mapping, get_internal_type(), or any override mechanism. '
                f'defaulting to "string"'
            )
            return build_basic_type(OpenApiTypes.STR)

    def _map_serializer_field(self, field, direction):
        if has_override(field, 'field'):
            override = get_override(field, 'field')
            if is_basic_type(override):
                return build_basic_type(override)
            else:
                return self._map_serializer_field(override, direction)

        meta = self._get_serializer_field_meta(field)

        serializer_field_extension = OpenApiSerializerFieldExtension.get_match(field)
        if serializer_field_extension:
            schema = serializer_field_extension.map_serializer_field(self, direction)
            return append_meta(schema, meta)

        # nested serializer
        if isinstance(field, serializers.Serializer):
            schema = self.resolve_serializer(field, direction).ref
            return append_meta(schema, meta)

        # nested serializer with many=True gets automatically replaced with ListSerializer
        if isinstance(field, serializers.ListSerializer):
            schema = self.resolve_serializer(field.child, direction).ref
            return append_meta(build_array_type(schema), meta)

        # Related fields.
        if isinstance(field, serializers.ManyRelatedField):
            schema = self._map_serializer_field(field.child_relation, direction)
            # remove hand-over initkwargs applying only to outer scope
            schema.pop('description', None)
            return append_meta(build_array_type(schema), meta)

        if isinstance(field, serializers.PrimaryKeyRelatedField):
            # read_only fields do not have a Manager by design. go around and get field
            # from parent. also avoid calling Manager. __bool__ as it might be customized
            # to hit the database.
            if getattr(field, 'queryset', None) is not None:
                schema = self._map_model_field(field.queryset.model._meta.pk, direction)
            else:
                schema = self._map_model_field(field.parent.Meta.model._meta.pk, direction)
            # primary keys are usually non-editable (readOnly=True) and map_model_field correctly
            # signals that attribute. however this does not apply in the context of relations.
            schema.pop('readOnly', None)
            return append_meta(schema, meta)

        if isinstance(field, serializers.StringRelatedField):
            return append_meta(build_basic_type(OpenApiTypes.STR), meta)

        if isinstance(field, serializers.SlugRelatedField):
            return append_meta(build_basic_type(OpenApiTypes.STR), meta)

        if isinstance(field, serializers.HyperlinkedIdentityField):
            return append_meta(build_basic_type(OpenApiTypes.URI), meta)

        if isinstance(field, serializers.HyperlinkedRelatedField):
            return append_meta(build_basic_type(OpenApiTypes.URI), meta)

        # ChoiceFields (single and multiple).
        # Q:
        # - Is 'type' required?
        # - can we determine the TYPE of a choicefield?
        if isinstance(field, serializers.MultipleChoiceField):
            return append_meta(build_array_type(self._map_choicefield(field)), meta)

        if isinstance(field, serializers.ChoiceField):
            return append_meta(self._map_choicefield(field), meta)

        if isinstance(field, serializers.ListField):
            schema = build_array_type({})
            # TODO check this
            if not isinstance(field.child, _UnvalidatedField):
                map_field = self._map_serializer_field(field.child, direction)
                items = {
                    "type": map_field.get('type')
                }
                if 'format' in map_field:
                    items['format'] = map_field.get('format')
                schema['items'] = items
            return append_meta(schema, meta)

        # DateField and DateTimeField type is string
        if isinstance(field, serializers.DateField):
            return append_meta(build_basic_type(OpenApiTypes.DATE), meta)

        if isinstance(field, serializers.DateTimeField):
            return append_meta(build_basic_type(OpenApiTypes.DATETIME), meta)

        if isinstance(field, serializers.TimeField):
            return append_meta(build_basic_type(OpenApiTypes.TIME), meta)

        if isinstance(field, serializers.EmailField):
            return append_meta(build_basic_type(OpenApiTypes.EMAIL), meta)

        if isinstance(field, serializers.URLField):
            return append_meta(build_basic_type(OpenApiTypes.URI), meta)

        if isinstance(field, serializers.UUIDField):
            return append_meta(build_basic_type(OpenApiTypes.UUID), meta)

        if isinstance(field, serializers.IPAddressField):
            # TODO this might be a DRF bug. protocol is not propagated to serializer although it
            #  should have been. results in always 'both' (thus no format)
            if 'ipv4' == field.protocol.lower():
                schema = build_basic_type(OpenApiTypes.IP4)
            elif 'ipv6' == field.protocol.lower():
                schema = build_basic_type(OpenApiTypes.IP6)
            else:
                schema = build_basic_type(OpenApiTypes.STR)
            return append_meta(schema, meta)

        # DecimalField has multipleOf based on decimal_places
        if isinstance(field, serializers.DecimalField):
            if getattr(field, 'coerce_to_string', api_settings.COERCE_DECIMAL_TO_STRING):
                content = {**build_basic_type(OpenApiTypes.STR), 'format': 'decimal'}
            else:
                content = build_basic_type(OpenApiTypes.DECIMAL)

            if field.decimal_places:
                content['multipleOf'] = float('.' + (field.decimal_places - 1) * '0' + '1')
            if field.max_whole_digits:
                content['maximum'] = int(field.max_whole_digits * '9') + 1
                content['minimum'] = -content['maximum']
            self._map_min_max(field, content)
            return append_meta(content, meta)

        if isinstance(field, serializers.FloatField):
            content = build_basic_type(OpenApiTypes.FLOAT)
            self._map_min_max(field, content)
            return append_meta(content, meta)

        if isinstance(field, serializers.IntegerField):
            content = build_basic_type(OpenApiTypes.INT)
            self._map_min_max(field, content)
            # 2147483647 is max for int32_size, so we use int64 for format
            if int(content.get('maximum', 0)) > 2147483647 or int(content.get('minimum', 0)) > 2147483647:
                content['format'] = 'int64'
            return append_meta(content, meta)

        if isinstance(field, serializers.FileField):
            if spectacular_settings.COMPONENT_SPLIT_REQUEST and direction == 'request':
                content = build_basic_type(OpenApiTypes.FILE)
            else:
                use_url = getattr(field, 'use_url', api_settings.UPLOADED_FILES_USE_URL)
                content = build_basic_type(OpenApiTypes.URI if use_url else OpenApiTypes.FILE)
            return append_meta(content, meta)

        if isinstance(field, serializers.SerializerMethodField):
            method = getattr(field.parent, field.method_name)
            return append_meta(self._map_response_type_hint(method), meta)

        if anyisinstance(field, [serializers.BooleanField, serializers.NullBooleanField]):
            return append_meta(build_basic_type(OpenApiTypes.BOOL), meta)

        if anyisinstance(field, [serializers.JSONField, serializers.DictField, serializers.HStoreField]):
            return append_meta(build_basic_type(OpenApiTypes.OBJECT), meta)

        if isinstance(field, serializers.CharField):
            return append_meta(build_basic_type(OpenApiTypes.STR), meta)

        if isinstance(field, serializers.ReadOnlyField):
            # direct source from the serializer
            assert field.source_attrs, 'ReadOnlyField needs a proper source'
            target = follow_field_source(field.parent.Meta.model, field.source_attrs)

            if callable(target):
                schema = self._map_response_type_hint(target)
            elif isinstance(target, models.Field):
                schema = self._map_model_field(target, direction)
            else:
                assert False, 'ReadOnlyField target must be property or model field'
            return append_meta(schema, meta)

        # DRF was not able to match the model field to an explicit SerializerField and therefore
        # used its generic fallback serializer field that simply wraps the model field.
        if isinstance(field, serializers.ModelField):
            schema = self._map_model_field(field.model_field, direction)
            return append_meta(schema, meta)

        warn(f'could not resolve serializer field {field}. defaulting to "string"')
        return append_meta(build_basic_type(OpenApiTypes.STR), meta)

    def _map_min_max(self, field, content):
        if field.max_value:
            content['maximum'] = field.max_value
        if field.min_value:
            content['minimum'] = field.min_value

    def _map_serializer(self, serializer, direction):
        serializer = force_instance(serializer)
        serializer_extension = OpenApiSerializerExtension.get_match(serializer)

        if serializer_extension:
            schema = serializer_extension.map_serializer(self, direction)
        else:
            schema = self._map_basic_serializer(serializer, direction)

        return self._postprocess_serializer_schema(schema, serializer, direction)

    def _postprocess_serializer_schema(self, schema, serializer, direction):
        """
        postprocess generated schema for component splitting, if enabled.
        does only apply to direct component schemas and not intermediate schemas
        like components composed of sub-component via e.g. oneOf.
        """
        if not spectacular_settings.COMPONENT_SPLIT_REQUEST:
            return schema

        properties = schema.get('properties', [])
        required = schema.get('required', [])

        for prop_name in list(properties):
            if direction == 'request' and properties[prop_name].get('readOnly'):
                del schema['properties'][prop_name]
                if prop_name in required:
                    required.remove(prop_name)
            if direction == 'response' and properties[prop_name].get('writeOnly'):
                del schema['properties'][prop_name]
                if prop_name in required:
                    required.remove(prop_name)

        return schema

    def _get_serializer_field_meta(self, field):
        meta = {}
        if field.read_only:
            meta['readOnly'] = True
        if field.write_only:
            meta['writeOnly'] = True
        if field.allow_null:
            meta['nullable'] = True
        if field.default is not None and field.default != empty and not callable(field.default):
            default = field.to_representation(field.default)
            if isinstance(default, set):
                default = list(default)
            meta['default'] = default
        if field.help_text:
            meta['description'] = str(field.help_text)
        return meta

    def _map_basic_serializer(self, serializer, direction):
        serializer = force_instance(serializer)
        required = set()
        properties = {}

        for field in serializer.fields.values():
            if isinstance(field, serializers.HiddenField):
                continue

            schema = self._map_serializer_field(field, direction)

            if field.required or schema.get('readOnly'):
                required.add(field.field_name)

            self._map_field_validators(field, schema)

            properties[field.field_name] = safe_ref(schema)

        if spectacular_settings.COMPONENT_SPLIT_PATCH:
            if self.method == 'PATCH' and direction == 'request':
                required = []

        return build_object_type(
            properties=properties,
            required=required,
            description=inspect.getdoc(serializer),
        )

    def _map_field_validators(self, field, schema):
        for v in field.validators:
            # https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.3.md#data-types
            if isinstance(v, validators.EmailValidator):
                schema['format'] = 'email'
            if isinstance(v, validators.URLValidator):
                schema['format'] = 'uri'
            if isinstance(v, validators.RegexValidator):
                schema['pattern'] = v.regex.pattern
            elif isinstance(v, validators.MaxLengthValidator):
                attr_name = 'maxLength'
                if isinstance(field, serializers.ListField):
                    attr_name = 'maxItems'
                schema[attr_name] = v.limit_value
            elif isinstance(v, validators.MinLengthValidator):
                attr_name = 'minLength'
                if isinstance(field, serializers.ListField):
                    attr_name = 'minItems'
                schema[attr_name] = v.limit_value
            elif isinstance(v, validators.MaxValueValidator):
                schema['maximum'] = v.limit_value
            elif isinstance(v, validators.MinValueValidator):
                schema['minimum'] = v.limit_value
            elif isinstance(v, validators.DecimalValidator):
                if v.decimal_places:
                    schema['multipleOf'] = float('.' + (v.decimal_places - 1) * '0' + '1')
                if v.max_digits:
                    digits = v.max_digits
                    if v.decimal_places is not None and v.decimal_places > 0:
                        digits -= v.decimal_places
                    schema['maximum'] = int(digits * '9') + 1
                    schema['minimum'] = -schema['maximum']

    def _map_response_type_hint(self, method):
        hint = get_override(method, 'field') or typing.get_type_hints(method).get('return')

        if is_serializer(hint) or is_field(hint):
            return self._map_serializer_field(force_instance(hint), 'response')
        elif is_basic_type(hint):
            return build_basic_type(hint)
        elif getattr(hint, '__origin__', None) is typing.Union:
            if type(None) == hint.__args__[1] and len(hint.__args__) == 2:
                schema = build_basic_type(hint.__args__[0])
                schema['nullable'] = True
                return schema
            else:
                warn(f'type hint {hint} not supported yet. defaulting to "string"')
                return build_basic_type(OpenApiTypes.STR)
        else:
            warn(f'type hint for function "{method.__name__}" is unknown. defaulting to string.')
            return build_basic_type(OpenApiTypes.STR)

    def _get_paginator(self):
        pagination_class = getattr(self.view, 'pagination_class', None)
        if pagination_class:
            return pagination_class()
        return None

    def map_parsers(self):
        return list(map(attrgetter('media_type'), self.view.parser_classes))

    def map_renderers(self):
        media_types = []
        for renderer in self.view.renderer_classes:
            # BrowsableAPIRenderer not relevant to OpenAPI spec
            if renderer == renderers.BrowsableAPIRenderer:
                continue
            media_types.append(renderer.media_type)
        return media_types

    def _get_serializer(self):
        view = self.view
        try:
            if isinstance(view, GenericAPIView):
                # try to circumvent queryset issues with calling get_serializer. if view has NOT
                # overridden get_serializer, its safe to use get_serializer_class.
                if view.__class__.get_serializer == GenericAPIView.get_serializer:
                    return view.get_serializer_class()()
                return view.get_serializer()
            elif isinstance(view, APIView):
                # APIView does not implement the required interface, but be lenient and make
                # good guesses before giving up and emitting a warning.
                if callable(getattr(view, 'get_serializer', None)):
                    return view.get_serializer()
                elif callable(getattr(view, 'get_serializer_class', None)):
                    return view.get_serializer_class()()
                elif hasattr(view, 'serializer_class'):
                    return view.serializer_class
                else:
                    error(
                        f'Unable to guess serializer for {view.__class__.__name__}. This is graceful '
                        f'fallback handling for APIViews. Consider using GenericAPIView as view base '
                        f'class, if view is under your control. ignoring view for now. '
                    )
            else:
                error('Encountered unknown view base class. please report this issue. ignoring for now')
        except Exception as exc:
            error(
                f'Exception raised while getting serializer from {view.__class__.__name__}. Hint: '
                f'Is get_serializer_class() returning None or is get_queryset() not working without '
                f'a request? Ignoring the view for now. (Exception: {exc})'
            )

    def _get_request_body(self):
        # only unsafe methods can have a body
        if self.method not in ('PUT', 'PATCH', 'POST'):
            return None

        serializer = force_instance(self.get_request_serializer())

        request_body_required = False
        if isinstance(serializer, serializers.ListSerializer):
            component = self.resolve_serializer(serializer.child, 'request')
            schema = build_array_type(component.ref)
            request_body_required = True
        elif is_serializer(serializer):
            component = self.resolve_serializer(serializer, 'request')
            if not component:
                # serializer is empty so skip content enumeration
                return None
            schema = component.ref
            # request body is only required if any required property is not read-only
            readonly_props = [
                p for p, s in component.schema.get('properties', {}).items() if s.get('readOnly')
            ]
            required_props = component.schema.get('required', [])
            request_body_required = any(req not in readonly_props for req in required_props)
        elif is_basic_type(serializer):
            schema = build_basic_type(serializer)
            if not schema:
                return None
        else:
            warn(
                f'could not resolve request body for {self.method} {self.path}. defaulting to generic '
                'free-form object. (maybe annotate a Serializer class?)'
            )
            schema = build_object_type(
                additionalProperties={},
                description='Unspecified request body',
            )

        request_body = {
            'content': {
                request_media_types: {'schema': schema} for request_media_types in self.map_parsers()
            }
        }
        if request_body_required:
            request_body['required'] = request_body_required

        return request_body

    def _get_response_bodies(self):
        response_serializers = self.get_response_serializers()

        if is_serializer(response_serializers) or is_basic_type(response_serializers):
            if self.method == 'DELETE':
                return {'204': {'description': 'No response body'}}
            return {'200': self._get_response_for_code(response_serializers)}
        elif isinstance(response_serializers, dict):
            # custom handling for overriding default return codes with @extend_schema
            return {
                str(code): self._get_response_for_code(serializer)
                for code, serializer in response_serializers.items()
            }
        else:
            warn(
                f'could not resolve "{response_serializers}" for {self.method} {self.path}. '
                f'Expected either a serializer or some supported override mechanism. '
                f'defaulting to generic free-form object.'
            )
            schema = build_basic_type(OpenApiTypes.OBJECT)
            schema['description'] = 'Unspecified response body'
            return {'200': self._get_response_for_code(schema)}

    def _get_response_for_code(self, serializer):
        serializer = force_instance(serializer)

        if not serializer:
            return {'description': 'No response body'}
        elif isinstance(serializer, serializers.ListSerializer):
            schema = self.resolve_serializer(serializer.child, 'response').ref
        elif is_serializer(serializer):
            component = self.resolve_serializer(serializer, 'response')
            if not component:
                return {'description': 'No response body'}
            schema = component.ref
        elif is_basic_type(serializer):
            schema = build_basic_type(serializer)
        elif isinstance(serializer, dict):
            # bypass processing and use given schema directly
            schema = serializer
        else:
            warn(
                f'could not resolve "{serializer}" for {self.method} {self.path}. Expected either '
                f'a serializer or some supported override mechanism. defaulting to '
                f'generic free-form object.'
            )
            schema = build_basic_type(OpenApiTypes.OBJECT)
            schema['description'] = 'Unspecified response body'

        if self._is_list_view(serializer) and not get_override(serializer, 'many') is False:
            schema = build_array_type(schema)
            paginator = self._get_paginator()
            if paginator:
                schema = paginator.get_paginated_response_schema(schema)

        return {
            'content': {
                mt: {'schema': schema} for mt in self.map_renderers()
            },
            # Description is required by spec, but descriptions for each response code don't really
            # fit into our model. Description is therefore put into the higher level slots.
            # https://github.com/OAI/OpenAPI-Specification/blob/master/versions/3.0.3.md#responseObject
            'description': ''
        }

    def _get_serializer_name(self, serializer, direction):
        serializer_extension = OpenApiSerializerExtension.get_match(serializer)
        if serializer_extension and serializer_extension.get_name():
            # library override mechanisms
            name = serializer_extension.get_name()
        elif getattr(getattr(serializer, 'Meta', None), 'ref_name', None) is not None:
            # local override mechanisms. for compatibility with drf-yasg we support meta ref_name,
            # though we do not support the serializer inlining feature.
            # https://drf-yasg.readthedocs.io/en/stable/custom_spec.html#serializer-meta-nested-class
            name = serializer.Meta.ref_name
        else:
            name = serializer.__class__.__name__

        if name.endswith('Serializer'):
            name = name[:-10]

        if self.method == 'PATCH' and spectacular_settings.COMPONENT_SPLIT_PATCH:
            if not serializer.read_only and direction == 'request':
                name = 'Patched' + name

        if direction == 'request' and spectacular_settings.COMPONENT_SPLIT_REQUEST:
            name = name + 'Request'

        return name

    def resolve_serializer(self, serializer, direction) -> ResolvedComponent:
        assert is_serializer(serializer)
        serializer = force_instance(serializer)

        component = ResolvedComponent(
            name=self._get_serializer_name(serializer, direction),
            type=ResolvedComponent.SCHEMA,
            object=serializer,
        )
        if component in self.registry:
            return self.registry[component]  # return component with schema

        self.registry.register(component)
        component.schema = self._map_serializer(serializer, direction)
        # 4 cases:
        #   1. polymorphic container component -> use
        #   2. concrete component with properties -> use
        #   3. concrete component without properties -> prob. transactional so discard
        #   4. explicit list component -> demultiplexed at usage location so discard
        keep_component = (
            any(nest_tag in component.schema for nest_tag in ['oneOf', 'allOf', 'anyOf'])
            or component.schema.get('properties', {})
        )
        if not keep_component:
            del self.registry[component]
            return ResolvedComponent(None, None)  # sentinel
        return component
