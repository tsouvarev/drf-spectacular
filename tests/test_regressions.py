import uuid
from unittest import mock

from django.conf.urls import url
from django.db.models import fields
from django.db import models
from django.urls import path, re_path
from rest_framework import serializers, viewsets, mixins, routers, views, generics, parsers
from rest_framework.decorators import action, api_view
from rest_framework.views import APIView

from drf_spectacular.extensions import OpenApiSerializerExtension
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_serializer
from drf_spectacular.validation import validate_schema
from tests import generate_schema, get_response_schema, get_request_schema


def test_primary_key_read_only_queryset_not_found(no_warnings):
    # the culprit - looks like a feature not a bug.
    # https://github.com/encode/django-rest-framework/blame/4d9f9eb192c5c1ffe4fa9210b90b9adbb00c3fdd/rest_framework/utils/field_mapping.py#L271

    class M1(models.Model):
        pass  # pragma: no cover

    class M2(models.Model):
        m1_r = models.ForeignKey(M1, on_delete=models.CASCADE)
        m1_rw = models.ForeignKey(M1, on_delete=models.CASCADE)

    class M2Serializer(serializers.ModelSerializer):
        class Meta:
            fields = ['m1_rw', 'm1_r']
            read_only_fields = ['m1_r']  # this produces the bug
            model = M2

    class M2Viewset(viewsets.ReadOnlyModelViewSet):
        serializer_class = M2Serializer
        queryset = M2.objects.none()

    schema = generate_schema('m2', M2Viewset)
    props = schema['components']['schemas']['M2']['properties']
    assert props['m1_rw']['type'] == 'integer'
    assert props['m1_r']['type'] == 'integer'


def test_path_implicit_required(no_warnings):
    class M2Serializer(serializers.Serializer):
        pass  # pragma: no cover

    class M2Viewset(viewsets.GenericViewSet):
        serializer_class = M2Serializer

        @extend_schema(parameters=[OpenApiParameter('id', str, 'path')])
        def retrieve(self, request, *args, **kwargs):
            pass  # pragma: no cover

    generate_schema('m2', M2Viewset)


def test_free_form_responses(no_warnings):
    class XAPIView(APIView):
        @extend_schema(responses={200: OpenApiTypes.OBJECT})
        def get(self, request):
            pass  # pragma: no cover

    class YAPIView(APIView):
        @extend_schema(responses=OpenApiTypes.OBJECT)
        def get(self, request):
            pass  # pragma: no cover

    generator = SchemaGenerator(patterns=[
        url(r'^x$', XAPIView.as_view(), name='x'),
        url(r'^y$', YAPIView.as_view(), name='y'),
    ])
    schema = generator.get_schema(request=None, public=True)
    validate_schema(schema)


@mock.patch(
    target='drf_spectacular.settings.spectacular_settings.APPEND_COMPONENTS',
    new={'schemas': {'SomeExtraComponent': {'type': 'integer'}}}
)
def test_append_extra_components(no_warnings):
    class XSerializer(serializers.Serializer):
        id = serializers.UUIDField()

    class XAPIView(APIView):
        @extend_schema(responses={200: XSerializer})
        def get(self, request):
            pass  # pragma: no cover

    generator = SchemaGenerator(patterns=[
        url(r'^x$', XAPIView.as_view(), name='x'),
    ])
    schema = generator.get_schema(request=None, public=True)
    assert len(schema['components']['schemas']) == 2
    validate_schema(schema)


def test_serializer_retrieval_from_view(no_warnings):
    class UnusedSerializer(serializers.Serializer):
        pass  # pragma: no cover

    class XSerializer(serializers.Serializer):
        id = serializers.UUIDField()

    class YSerializer(serializers.Serializer):
        id = serializers.UUIDField()

    class X1Viewset(mixins.ListModelMixin, viewsets.GenericViewSet):
        serializer_class = UnusedSerializer

        def get_serializer(self):
            return XSerializer()

    class X2Viewset(mixins.ListModelMixin, viewsets.GenericViewSet):
        def get_serializer_class(self):
            return YSerializer

    router = routers.SimpleRouter()
    router.register('x1', X1Viewset, basename='x1')
    router.register('x2', X2Viewset, basename='x2')
    generator = SchemaGenerator(patterns=router.urls)
    schema = generator.get_schema(request=None, public=True)
    validate_schema(schema)
    assert len(schema['components']['schemas']) == 2
    assert 'Unused' not in schema['components']['schemas']


def test_retrieve_on_apiview_get(no_warnings):
    class XSerializer(serializers.Serializer):
        id = serializers.UUIDField()

    class XApiView(APIView):
        authentication_classes = []

        @extend_schema(
            parameters=[OpenApiParameter('id', OpenApiTypes.INT, OpenApiParameter.PATH)],
            responses={200: XSerializer},
        )
        def get(self, request):
            pass  # pragma: no cover

    schema = generate_schema('x', view=XApiView)
    operation = schema['paths']['/x']['get']
    assert operation['operationId'] == 'x_retrieve'
    operation_schema = get_response_schema(operation)
    assert '$ref' in operation_schema and 'type' not in operation_schema


def test_list_on_apiview_get(no_warnings):
    class XSerializer(serializers.Serializer):
        id = serializers.UUIDField()

    class XApiView(APIView):
        authentication_classes = []

        @extend_schema(
            parameters=[OpenApiParameter('id', OpenApiTypes.INT, OpenApiParameter.PATH)],
            responses={200: XSerializer(many=True)},
        )
        def get(self, request):
            pass  # pragma: no cover

    schema = generate_schema('x', view=XApiView)
    operation = schema['paths']['/x']['get']
    assert operation['operationId'] == 'x_list'
    operation_schema = get_response_schema(operation)
    assert operation_schema['type'] == 'array'


def test_multi_method_action(no_warnings):
    class DummySerializer(serializers.Serializer):
        id = serializers.UUIDField()

    class UpdateSerializer(serializers.Serializer):
        id = serializers.UUIDField()

    class CreateSerializer(serializers.Serializer):
        id = serializers.UUIDField()

    class XViewset(viewsets.GenericViewSet):
        serializer_class = DummySerializer

        # basic usage
        @extend_schema(request=UpdateSerializer, methods=['PUT'])
        @extend_schema(request=CreateSerializer, methods=['POST'])
        @action(detail=False, methods=['PUT', 'POST'])
        def multi(self, request, *args, **kwargs):
            pass  # pragma: no cover

        # bolt-on decorator variation
        @extend_schema(request=CreateSerializer)
        @action(detail=False, methods=['POST'])
        def multi2(self, request, *args, **kwargs):
            pass  # pragma: no cover

        @extend_schema(request=UpdateSerializer)
        @multi2.mapping.put
        def multi2put(self, request, *args, **kwargs):
            pass  # pragma: no cover

    schema = generate_schema('x', XViewset)

    def get_req_body(s):
        return s['requestBody']['content']['application/json']['schema']['$ref']

    assert get_req_body(schema['paths']['/x/multi/']['put']) == '#/components/schemas/Update'
    assert get_req_body(schema['paths']['/x/multi/']['post']) == '#/components/schemas/Create'

    assert get_req_body(schema['paths']['/x/multi2/']['put']) == '#/components/schemas/Update'
    assert get_req_body(schema['paths']['/x/multi2/']['post']) == '#/components/schemas/Create'


def test_serializer_class_on_apiview(no_warnings):
    class XSerializer(serializers.Serializer):
        field = serializers.UUIDField()

    class XView(views.APIView):
        serializer_class = XSerializer  # not supported by DRF but pick it up anyway

        def get(self, request):
            pass  # pragma: no cover

        def post(self, request):
            pass  # pragma: no cover

    schema = generate_schema('x', view=XView)
    comp = '#/components/schemas/X'
    assert get_response_schema(schema['paths']['/x']['get'])['$ref'] == comp
    assert get_response_schema(schema['paths']['/x']['post'])['$ref'] == comp
    assert schema['paths']['/x']['post']['requestBody']['content']['application/json']['schema']['$ref'] == comp


def test_customized_list_serializer():
    class X(models.Model):
        position = models.IntegerField()

    class XSerializer(serializers.ModelSerializer):
        class Meta:
            model = X
            fields = ("id", "position")

    class XListUpdateSerializer(serializers.ListSerializer):
        child = XSerializer()

    class XAPIView(generics.GenericAPIView):
        model = X
        serializer_class = XListUpdateSerializer

        def put(self, request, *args, **kwargs):
            pass  # pragma: no cover

    schema = generate_schema('x', view=XAPIView)
    operation = schema['paths']['/x']['put']
    comp = '#/components/schemas/X'

    assert get_request_schema(operation)['type'] == 'array'
    assert get_request_schema(operation)['items']['$ref'] == comp
    assert get_response_schema(operation)['type'] == 'array'
    assert get_response_schema(operation)['items']['$ref'] == comp

    assert operation['operationId'] == 'x_update'
    assert len(schema['components']['schemas']) == 1 and 'X' in schema['components']['schemas']


def test_api_view_decorator(no_warnings):

    @extend_schema(responses=OpenApiTypes.FLOAT)
    @api_view(['GET'])
    def pi(request):
        pass  # pragma: no cover

    schema = generate_schema('x', view_function=pi)
    operation = schema['paths']['/x']['get']
    assert get_response_schema(operation)['type'] == 'number'


def test_api_view_decorator_multi(no_warnings):

    @extend_schema(request=OpenApiTypes.FLOAT, responses=OpenApiTypes.INT, methods=['POST'])
    @extend_schema(responses=OpenApiTypes.FLOAT, methods=['GET'])
    @api_view(['GET', 'POST'])
    def pi(request):
        pass  # pragma: no cover

    schema = generate_schema('x', view_function=pi)
    operation = schema['paths']['/x']['get']
    assert get_response_schema(operation)['type'] == 'number'
    operation = schema['paths']['/x']['post']
    assert get_request_schema(operation)['type'] == 'number'
    assert get_response_schema(operation)['type'] == 'integer'


def test_pk_and_no_id(no_warnings):
    class XModel(models.Model):
        id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class YModel(models.Model):
        x = models.OneToOneField(XModel, primary_key=True, on_delete=models.CASCADE)

    class YSerializer(serializers.ModelSerializer):
        class Meta:
            model = YModel
            fields = '__all__'

    class YViewSet(viewsets.ReadOnlyModelViewSet):
        serializer_class = YSerializer
        queryset = YModel.objects.all()

    schema = generate_schema('y', YViewSet)
    assert schema['components']['schemas']['Y']['properties']['x']['format'] == 'uuid'


def test_drf_format_suffix_parameter(no_warnings):
    from rest_framework.urlpatterns import format_suffix_patterns

    @extend_schema(responses=OpenApiTypes.FLOAT)
    @api_view(['GET'])
    def pi(request, format=None):
        pass  # pragma: no cover

    urlpatterns = [path('pi', pi)]
    urlpatterns = format_suffix_patterns(urlpatterns, allowed=['json', 'html'])

    generator = SchemaGenerator(patterns=urlpatterns)
    schema = generator.get_schema(request=None, public=True)

    assert len(schema['paths']) == 2
    format_parameter = schema['paths']['/pi{format}']['get']['parameters'][0]
    assert format_parameter['name'] == 'format'
    assert format_parameter['required'] is False
    assert format_parameter['in'] == 'path'


def test_regex_path_parameter_discovery(no_warnings):
    @extend_schema(responses=OpenApiTypes.FLOAT)
    @api_view(['GET'])
    def pi(request, foo):
        pass  # pragma: no cover

    urlpatterns = [re_path(r'^/pi/<int:precision>', pi)]
    generator = SchemaGenerator(patterns=urlpatterns)
    schema = generator.get_schema(request=None, public=True)
    parameter = schema['paths']['/pi/{precision}']['get']['parameters'][0]
    assert parameter['name'] == 'precision'
    assert parameter['in'] == 'path'
    assert parameter['schema']['type'] == 'integer'


def test_lib_serializer_naming_collision_resolution(no_warnings):
    """ parity test in tests.test_warnings.test_serializer_name_reuse """
    def x_lib1():
        class XSerializer(serializers.Serializer):
            x = serializers.UUIDField()

        return XSerializer

    def x_lib2():
        class XSerializer(serializers.Serializer):
            x = serializers.IntegerField()

        return XSerializer

    x_lib1, x_lib2 = x_lib1(), x_lib2()

    class XAPIView(APIView):
        @extend_schema(request=x_lib1, responses=x_lib2)
        def post(self, request):
            pass  # pragma: no cover

    class Lib2XSerializerRename(OpenApiSerializerExtension):
        target_class = x_lib2  # also accepts import strings

        def get_name(self):
            return 'RenamedLib2X'

    schema = generate_schema('x', view=XAPIView)

    operation = schema['paths']['/x']['post']
    assert get_request_schema(operation)['$ref'] == '#/components/schemas/X'
    assert get_response_schema(operation)['$ref'] == '#/components/schemas/RenamedLib2X'


def test_owned_serializer_naming_override_with_ref_name(no_warnings):
    def x_owned1():
        class XSerializer(serializers.Serializer):
            x = serializers.UUIDField()

        return XSerializer

    def x_owned2():
        class XSerializer(serializers.Serializer):
            x = serializers.IntegerField()

            class Meta:
                ref_name = 'Y'

        return XSerializer

    x_owned1, x_owned2 = x_owned1(), x_owned2()

    class XAPIView(APIView):
        @extend_schema(request=x_owned1, responses=x_owned2)
        def post(self, request):
            pass  # pragma: no cover

    schema = generate_schema('x', view=XAPIView)

    operation = schema['paths']['/x']['post']
    assert get_request_schema(operation)['$ref'] == '#/components/schemas/X'
    assert get_response_schema(operation)['$ref'] == '#/components/schemas/Y'


def test_custom_model_field_from_typed_field(no_warnings):
    class CustomIntegerField(fields.IntegerField):
        pass  # pragma: no cover

    class CustomTypedFieldModel(models.Model):
        custom_int_field = CustomIntegerField()

    class XSerializer(serializers.ModelSerializer):
        class Meta:
            model = CustomTypedFieldModel
            fields = '__all__'

    class XAPIView(APIView):
        @extend_schema(responses=XSerializer)
        def get(self, request):
            pass  # pragma: no cover

    schema = generate_schema('x', view=XAPIView)
    component = schema['components']['schemas']['X']
    assert component['properties']['custom_int_field']['type'] == 'integer'


def test_custom_model_field_from_base_field(no_warnings):
    class CustomIntegerField(fields.Field):
        def get_internal_type(self):
            return 'IntegerField'

    class CustomBaseFieldModel(models.Model):
        custom_int_field = CustomIntegerField()

    class XSerializer(serializers.ModelSerializer):
        class Meta:
            model = CustomBaseFieldModel
            fields = '__all__'

    class XAPIView(APIView):
        @extend_schema(responses=XSerializer)
        def get(self, request):
            pass  # pragma: no cover

    schema = generate_schema('x', view=XAPIView)
    component = schema['components']['schemas']['X']
    assert component['properties']['custom_int_field']['type'] == 'integer'


def test_follow_field_source_through_intermediate_property_or_function(no_warnings):
    class FieldSourceTraversalModel2(models.Model):
        y = models.IntegerField(choices=[(1, '1'), (2, '2'), (3, '3')])

    class FieldSourceTraversalModel1(models.Model):
        @property
        def prop(self) -> FieldSourceTraversalModel2:  # property is required for traversal
            return  # pragma: no cover

        def func(self) -> FieldSourceTraversalModel2:  # property is required for traversal
            return  # pragma: no cover

    class XSerializer(serializers.ModelSerializer):
        prop = serializers.ReadOnlyField(source='prop.y')
        func = serializers.ReadOnlyField(source='func.y')

        class Meta:
            model = FieldSourceTraversalModel1
            fields = '__all__'

    class XAPIView(APIView):
        @extend_schema(responses=XSerializer)
        def get(self, request):
            pass  # pragma: no cover

    # this checks if field type is correctly estimated AND field was initialized
    # with the model parameters (choices)
    schema = generate_schema('x', view=XAPIView)
    assert schema['components']['schemas']['X']['properties']['func']['readOnly'] is True
    assert schema['components']['schemas']['X']['properties']['prop']['readOnly'] is True
    assert 'enum' in schema['components']['schemas']['PropEnum']
    assert 'enum' in schema['components']['schemas']['FuncEnum']
    assert schema['components']['schemas']['PropEnum']['type'] == 'integer'
    assert schema['components']['schemas']['FuncEnum']['type'] == 'integer'


def test_viewset_list_with_envelope(no_warnings):
    class XSerializer(serializers.Serializer):
        x = serializers.IntegerField()

    def enveloper(serializer_class, list):
        @extend_schema_serializer(many=False)
        class EnvelopeSerializer(serializers.Serializer):
            status = serializers.BooleanField()
            data = XSerializer(many=list)

            class Meta:
                ref_name = 'Enveloped{}{}'.format(
                    serializer_class.__name__.replace("Serializer", ""),
                    "List" if list else "",
                )
        return EnvelopeSerializer

    class XViewset(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
        @extend_schema(responses=enveloper(XSerializer, True))
        def list(self, request, *args, **kwargs):
            return super().list(request, *args, **kwargs)  # pragma: no cover

        @extend_schema(
            responses=enveloper(XSerializer, False),
            parameters=[OpenApiParameter('id', int, OpenApiParameter.PATH)],
        )
        def retrieve(self, request, *args, **kwargs):
            return super().retrieve(request, *args, **kwargs)  # pragma: no cover

    schema = generate_schema('x', viewset=XViewset)

    operation_list = schema['paths']['/x/']['get']
    assert operation_list['operationId'] == 'x_list'
    assert get_response_schema(operation_list)['$ref'] == '#/components/schemas/EnvelopedXList'

    operation_retrieve = schema['paths']['/x/{id}/']['get']
    assert operation_retrieve['operationId'] == 'x_retrieve'
    assert get_response_schema(operation_retrieve)['$ref'] == '#/components/schemas/EnvelopedX'


@mock.patch('drf_spectacular.settings.spectacular_settings.COMPONENT_SPLIT_REQUEST', True)
def test_component_split_request():
    class XSerializer(serializers.Serializer):
        ro = serializers.IntegerField(read_only=True)
        rw = serializers.IntegerField()
        wo = serializers.IntegerField(write_only=True)

    @extend_schema(request=XSerializer, responses=XSerializer)
    @api_view(['POST'])
    def pi(request, format=None):
        pass  # pragma: no cover

    schema = generate_schema('/x', view_function=pi)

    operation = schema['paths']['/x']['post']

    assert get_response_schema(operation)['$ref'] == '#/components/schemas/X'
    assert get_request_schema(operation)['$ref'] == '#/components/schemas/XRequest'
    assert len(schema['components']['schemas']['X']['properties']) == 2
    assert 'wo' not in schema['components']['schemas']['X']['properties']
    assert len(schema['components']['schemas']['XRequest']['properties']) == 2
    assert 'ro' not in schema['components']['schemas']['XRequest']['properties']


def test_list_api_view(no_warnings):
    class XSerializer(serializers.Serializer):
        id = serializers.IntegerField()

    class XView(generics.ListAPIView):
        serializer_class = XSerializer

    schema = generate_schema('/x', view=XView)
    operation = schema['paths']['/x']['get']
    assert operation['operationId'] == 'x_list'
    assert get_response_schema(operation)['type'] == 'array'


@mock.patch('drf_spectacular.settings.spectacular_settings.COMPONENT_SPLIT_REQUEST', True)
def test_file_field_duality_on_split_request(no_warnings):
    class XSerializer(serializers.Serializer):
        file = serializers.FileField()

    class XView(generics.ListCreateAPIView):
        serializer_class = XSerializer
        parser_classes = [parsers.MultiPartParser]

    schema = generate_schema('/x', view=XView)
    assert get_response_schema(
        schema['paths']['/x']['get']
    )['items']['$ref'] == '#/components/schemas/X'
    assert get_request_schema(
        schema['paths']['/x']['post'], content_type='multipart/form-data'
    )['$ref'] == '#/components/schemas/XRequest'

    assert schema['components']['schemas']['X']['properties']['file']['format'] == 'uri'
    assert schema['components']['schemas']['XRequest']['properties']['file']['type'] == 'file'
