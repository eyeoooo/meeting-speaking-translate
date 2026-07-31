// 给 BlackHole 套语义名聚合设备：Teams 选设备时看到的是人话名字
// （「会议助手·扬声器」「会议助手·麦克风」），音频原样流进底下的
// BlackHole，bridge 的关键字匹配不受影响。
//
// 为什么不直接改 BlackHole 的设备名：名字是编译期写死的（要从源码
// 重编译），而且 bridge/doctor 按 "BlackHole 2ch"/"BlackHole 16ch"
// 关键字锁定设备，改了名产品就找不到设备了。聚合设备是 macOS 原生
// 机制（音频 MIDI 设置同款 API），持久化在系统音频偏好里。
//
// 自定义名字绝不能含 "BlackHole"——doctor 的"恰好匹配 1 个"启动
// 门禁会因此失败。
//
// 用法: swift tools/make_named_devices.swift            创建
//       swift tools/make_named_devices.swift --undo     删除
//       可选 --speaker-name/--mic-name 覆盖默认名字
import CoreAudio
import Foundation

func allDeviceIDs() -> [AudioObjectID] {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr
    else { return [] }
    var ids = [AudioObjectID](
        repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr
    else { return [] }
    return ids
}

func stringProp(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var addr = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var value: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr,
          let v = value
    else { return nil }
    return v.takeRetainedValue() as String
}

func findDevice(named name: String) -> (id: AudioObjectID, uid: String)? {
    for id in allDeviceIDs() {
        if stringProp(id, kAudioObjectPropertyName) == name,
           let uid = stringProp(id, kAudioDevicePropertyDeviceUID) {
            return (id, uid)
        }
    }
    return nil
}

struct Wrapper {
    let displayName: String
    let aggregateUID: String  // 聚合设备的稳定标识，重复运行不重复创建
    let subDeviceName: String
}

func argValue(_ flag: String) -> String? {
    let args = CommandLine.arguments
    guard let i = args.firstIndex(of: flag), i + 1 < args.count else { return nil }
    return args[i + 1]
}

let speakerName = argValue("--speaker-name") ?? "会议助手·扬声器"
let micName = argValue("--mic-name") ?? "会议助手·麦克风"
for name in [speakerName, micName] where name.lowercased().contains("blackhole") {
    print("名字不能含 \"BlackHole\"（会破坏 doctor 的唯一匹配门禁）：\(name)")
    exit(1)
}

let wrappers = [
    Wrapper(displayName: speakerName,
            aggregateUID: "meeting.assistant.speaker.wrap",
            subDeviceName: "BlackHole 2ch"),
    Wrapper(displayName: micName,
            aggregateUID: "meeting.assistant.mic.wrap",
            subDeviceName: "BlackHole 16ch"),
]

let undo = CommandLine.arguments.contains("--undo")

for w in wrappers {
    if undo {
        if let existing = findDevice(named: w.displayName) {
            let status = AudioHardwareDestroyAggregateDevice(existing.id)
            print("删除 \(w.displayName): \(status == noErr ? "OK" : "错误 \(status)")")
        } else {
            print("删除 \(w.displayName): 不存在，跳过")
        }
        continue
    }
    if findDevice(named: w.displayName) != nil {
        print("\(w.displayName): 已存在，跳过")
        continue
    }
    guard let sub = findDevice(named: w.subDeviceName) else {
        print("\(w.displayName): 找不到底层设备 \(w.subDeviceName)——先 "
              + "brew install blackhole-2ch blackhole-16ch")
        continue
    }
    let desc: [String: Any] = [
        kAudioAggregateDeviceNameKey: w.displayName,
        kAudioAggregateDeviceUIDKey: w.aggregateUID,
        kAudioAggregateDeviceSubDeviceListKey: [
            [kAudioSubDeviceUIDKey: sub.uid]
        ],
    ]
    var aggID = AudioObjectID(0)
    let status = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &aggID)
    print("创建 \(w.displayName) ← \(w.subDeviceName): "
          + (status == noErr ? "OK" : "错误 \(status)"))
}
