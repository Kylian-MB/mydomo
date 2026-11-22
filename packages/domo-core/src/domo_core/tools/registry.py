"""The registry of the core's tools, and the tools a person is shown.

``TOOLS`` maps the name the model uses to the tool. The model is only shown the tools whose type
of action the person may do (``tools_for``): under the catalog, their role's rights and their own
(spec 5.9), whatever limit or rule comes with it. A call to any other name still reaches the
barrier (and is refused) or is reported as unknown: what the model is shown is a courtesy, the
barrier is the rule.

``InternalTool``, ``InvalidArguments`` and ``tools_for`` live in ``tools.context`` (the tools use
them: a chain of actions holds only the tools the person is shown) and are re-exported here.
"""

from collections.abc import Mapping
from types import MappingProxyType

from domo_core.tools.agreements import AgreementDrop, AgreementKeep
from domo_core.tools.chain import ChainContinue, DoInOrder
from domo_core.tools.confirmation_tools import (
    AskBeforeActing,
    ConfirmationAmend,
    ConfirmationExpire,
    ConfirmationRemind,
    ConfirmationRequireExplicit,
)
from domo_core.tools.context import InternalTool, InvalidArguments, tools_for
from domo_core.tools.delivery_tools import DeliveryLook
from domo_core.tools.devices import DeviceEntrust, DeviceList, DeviceRemove
from domo_core.tools.household_admin import (
    HouseholdCancelInvitation,
    HouseholdChangeRole,
    HouseholdInvite,
    HouseholdLeave,
    HouseholdRemove,
    HouseholdTransferOwnership,
    InvitationLinkAgain,
)
from domo_core.tools.household_read import HouseholdList
from domo_core.tools.household_return import HouseholdBringBack
from domo_core.tools.key_tools import KeyAdd, KeyLimit, KeyRemove, KeyShare
from domo_core.tools.keys_house import KeysHouse
from domo_core.tools.made_tools import (
    MadeToolBack,
    MadeToolBuild,
    MadeToolChange,
    MadeToolCheck,
    MadeToolFind,
    MadeToolLaunchWhen,
    MadeToolLook,
    MadeToolRemove,
    ToolSet,
    ToolsSettings,
)
from domo_core.tools.memory_tools import MemoryChange, MemoryForget, MemoryKeep, MemorySearch
from domo_core.tools.model_key import ModelRekey
from domo_core.tools.model_tools import (
    ModelsChoose,
    ModelsRefresh,
    ModelsSetLevel,
    ModelsStepUp,
)
from domo_core.tools.nickname_tools import NicknameForget, NicknameKeep
from domo_core.tools.notes import (
    NoteKeep,
    NoteLeave,
    NoteListSent,
    NoteShow,
    NoteTold,
    NoteWithdraw,
)
from domo_core.tools.plan_tools import PlanCancel, PlanChange, PlanSet
from domo_core.tools.policy_tools import PolicyShow, PolicyUpdate
from domo_core.tools.profile import DomoName, ProfileRename
from domo_core.tools.program_run import ProgramRun
from domo_core.tools.rights_tools import RightsApply, RightsPropose
from domo_core.tools.spending_tools import KeyAdmin, SpendingShow
from domo_core.tools.successors import (
    SuccessorsNoteDeath,
    SuccessorsSet,
    SuccessorsShow,
    SuccessorsUndoDeath,
)
from domo_core.tools.text_tools import (
    TextsFind,
    TextsForSomeone,
    TextsLanguage,
    TextsNote,
    TextsRestore,
    TextsRewrite,
    TextsRewriteLanguage,
    WritingChoose,
)
from domo_core.tools.trust_tools import TrustCorrect, TrustLook, TrustNote

__all__ = ["TOOLS", "InternalTool", "InvalidArguments", "tools_for"]

_TOOLS: tuple[InternalTool, ...] = (
    HouseholdList(),
    ProfileRename(),
    HouseholdLeave(),
    NoteLeave(),
    NoteListSent(),
    NoteWithdraw(),
    NoteKeep(),
    NoteTold(),
    NoteShow(),
    ConfirmationRemind(),
    ConfirmationExpire(),
    ConfirmationRequireExplicit(),
    ConfirmationAmend(),
    AskBeforeActing(),
    PolicyShow(),
    PolicyUpdate(),
    RightsPropose(),
    RightsApply(),
    HouseholdInvite(),
    HouseholdChangeRole(),
    HouseholdRemove(),
    HouseholdCancelInvitation(),
    InvitationLinkAgain(),
    HouseholdBringBack(),
    HouseholdTransferOwnership(),
    ModelRekey(),
    KeyAdd(),
    KeyRemove(),
    KeyShare(),
    KeyLimit(),
    KeyAdmin(),
    SpendingShow(),
    KeysHouse(),
    ModelsChoose(),
    ModelsStepUp(),
    ModelsSetLevel(),
    ModelsRefresh(),
    TextsFind(),
    TextsRewrite(),
    TextsRestore(),
    TextsLanguage(),
    TextsNote(),
    TextsForSomeone(),
    TextsRewriteLanguage(),
    WritingChoose(),
    SuccessorsShow(),
    SuccessorsSet(),
    SuccessorsNoteDeath(),
    SuccessorsUndoDeath(),
    DeviceEntrust(),
    DeviceList(),
    DeviceRemove(),
    MemoryKeep(),
    MemoryChange(),
    MemoryForget(),
    MemorySearch(),
    PlanSet(),
    PlanChange(),
    PlanCancel(),
    DomoName(),
    NicknameKeep(),
    NicknameForget(),
    DeliveryLook(),
    TrustNote(),
    TrustLook(),
    TrustCorrect(),
    MadeToolBuild(),
    MadeToolChange(),
    MadeToolRemove(),
    MadeToolLook(),
    MadeToolFind(),
    MadeToolLaunchWhen(),
    ToolsSettings(),
    ToolSet(),
    MadeToolBack(),
    MadeToolCheck(),
    ProgramRun(),
    AgreementKeep(),
    AgreementDrop(),
    DoInOrder(),
    ChainContinue(),
)

TOOLS: Mapping[str, InternalTool] = MappingProxyType(
    {tool.definition.name: tool for tool in _TOOLS}
)
