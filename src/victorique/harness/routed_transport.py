"""One bridge transport over two model services: the solver's, and a separate one for the checks.

The statement check (``alignment_review``) and the proof check
(``whole_proof_review``) go to the check service; every other role (planning,
solving, repair, critique, investigation, selection) goes to the solver's
service. Each service keeps its own transport state, authorization, requested
model and process ceiling; this class adds no launch, retry or budget of its
own. Observations keep their bytes and identifiers; only the adapter identity
in their metadata becomes this transport's, which the bridge was created with.
"""
from __future__ import annotations

ADAPTER_ID = "routed_cli"
VERSION = "routed_cli_v1"
CHECK_KINDS = ("alignment_review", "whole_proof_review")


def service_of(kind):
    """The configured service that handles a call of this action kind."""
    return "checks" if kind in CHECK_KINDS else "solver"


class RoutedTransport:
    """Routes each claimed request to the solver's or the check service's transport."""

    ADAPTER_ID = ADAPTER_ID

    def __init__(self, solver, checks):
        described = solver.describe(), checks.describe()
        if described[0]["mode"] != described[1]["mode"]:
            raise ValueError("the solver's and the check service's transports must have the same mode")
        self.solver, self.checks = solver, checks
        self.MODE = described[0]["mode"]

    @property
    def members(self):
        return [("solver", self.solver), ("checks", self.checks)]

    def describe(self):
        return {"adapter_id": ADAPTER_ID, "version": VERSION, "mode": self.MODE,
                "can_start": True, "can_observe_existing": True,
                "model_execution": self.solver.describe()["model_execution"],
                "fresh_context_required": True, "raw_response_preserved": True}

    def route(self, request):
        return self.checks if service_of(request.packet["binding"]["action_kind"]) == "checks" else self.solver

    def _own(self, observation):
        if observation is None:
            return None
        from victorique.harness_bridge import TransportObservation
        metadata = observation.metadata
        if type(metadata) is dict:
            metadata = {**metadata, "adapter_id": ADAPTER_ID, "adapter_version": VERSION}
        return TransportObservation(metadata, observation.raw_response, observation.raw_transport)

    def start_once(self, request):
        return self._own(self.route(request).start_once(request))

    def observe_existing(self, request):
        return self._own(self.route(request).observe_existing(request))

    def timeout_observation(self, request):
        return self._own(self.route(request).timeout_observation(request))

    def local_capture(self, request):
        return self.route(request).local_capture(request)


def members(adapter):
    """(service, transport) pairs of a loop's adapter: two for a routed run, else the one."""
    return adapter.members if isinstance(adapter, RoutedTransport) else [("solver", adapter)]
