import Registry from "../../../core/Registry";
import { MicrophoneModel } from "./model";
import { Microphone as HtxMicrophone } from "./view";

Registry.addTag("microphone", MicrophoneModel, HtxMicrophone);
Registry.addObjectType(MicrophoneModel);

export { MicrophoneModel, HtxMicrophone };
