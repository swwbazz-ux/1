import test from "node:test";
import assert from "node:assert/strict";
import {execFileSync} from "node:child_process";
import {mkdtempSync, mkdirSync, writeFileSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join, resolve} from "node:path";

test("proved channel leases slow safety polling, expire and preserve write retries", () => {
    const work = mkdtempSync(join(tmpdir(), "field-delivery-"));
    try {
        const folder = join(work, "ru/copperresources/mobile");
        mkdirSync(folder, {recursive:true});
        const source = join(folder, "FieldDeliveryPolicyTest.java");
        writeFileSync(source, `package ru.copperresources.mobile;
public class FieldDeliveryPolicyTest {
    public static void main(String[] args) {
        assert FieldDeliveryPolicy.nextCheck(100, true, false, 10000) == 10000;
        FieldDeliveryPolicy.recordSse(100);
        assert FieldDeliveryPolicy.nextCheck(200, true, false, 10000) == 60000;
        assert FieldDeliveryPolicy.nextCheck(200, false, false, 10000) == 10000;
        assert FieldDeliveryPolicy.nextCheck(200, true, true, 10000) == 10000;
        assert FieldDeliveryPolicy.nextCheck(35101, true, false, 10000) == 10000;
        assert FieldDeliveryPolicy.nextCheck(50, true, false, 10000) == 10000;
        FieldDeliveryPolicy.recordPush(40000);
        assert FieldDeliveryPolicy.nextCheck(41000, false, false, 10000) == 60000;
        assert FieldDeliveryPolicy.nextCheck(41000, true, false, 10000) == 10000;
        assert FieldDeliveryPolicy.nextCheck(41000, false, true, 10000) == 10000;
        assert FieldDeliveryPolicy.nextCheck(40000 + FieldDeliveryPolicy.PUSH_LEASE_MS + 1, false, false, 10000) == 10000;
    }
}
`);
        const policy = resolve(import.meta.dirname, "../android/app/src/main/java/ru/copperresources/mobile/FieldDeliveryPolicy.java");
        execFileSync("javac", ["-encoding","UTF-8","-d",work,policy,source], {stdio:"pipe"});
        execFileSync("java", ["-ea","-cp",work,"ru.copperresources.mobile.FieldDeliveryPolicyTest"], {stdio:"pipe"});
    } finally {
        rmSync(work,{recursive:true,force:true});
    }
});
