from rest_framework.permissions import BasePermission

from disturbance.helpers import (
    is_internal,
)

class ProposalInternalUserPermission(BasePermission):

    def has_permission(self, request, view):
        return is_internal(request)