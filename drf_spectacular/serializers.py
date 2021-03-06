from abc import abstractmethod
from typing import Optional, List

from drf_spectacular.plumbing import warn, force_instance, OpenApiGeneratorExtension


class OpenApiSerializerExtension(OpenApiGeneratorExtension['OpenApiSerializerExtension']):
    _registry: List['OpenApiSerializerExtension'] = []

    def get_name(self) -> Optional[str]:
        """ return str for overriding default name extraction """
        return None

    @abstractmethod
    def map_serializer(self, auto_schema, method: str):
        pass


class PolymorphicProxySerializerExtension(OpenApiSerializerExtension):
    target_class = 'drf_spectacular.utils.PolymorphicProxySerializer'

    def get_name(self):
        return self.target.component_name

    def map_serializer(self, auto_schema, method: str):
        """ custom handling for @extend_schema's injection of PolymorphicProxySerializer """
        serializer = self.target
        sub_components = []

        for sub_serializer in serializer.serializers:
            sub_serializer = force_instance(sub_serializer)
            resolved_sub_serializer = auto_schema.resolve_serializer(method, sub_serializer)

            try:
                discriminator_field = sub_serializer.fields[serializer.resource_type_field_name]
                resource_type = discriminator_field.to_representation(None)
            except:  # noqa: E722
                warn(
                    f'sub-serializer {resolved_sub_serializer.name} of {serializer.component_name} '
                    f'must contain the discriminator field "{serializer.resource_type_field_name}". '
                    f'defaulting to sub-serializer name, but schema will likely not match the API.'
                )
                resource_type = resolved_sub_serializer.name

            sub_components.append((resource_type, resolved_sub_serializer.ref))

        return {
            'oneOf': [ref for _, ref in sub_components],
            'discriminator': {
                'propertyName': serializer.resource_type_field_name,
                'mapping': {resource_type: ref['$ref'] for resource_type, ref in sub_components}
            }
        }
